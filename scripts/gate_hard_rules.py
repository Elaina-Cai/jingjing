#!/usr/bin/env python
"""Phase 5 Layer-1 hard-rule gate: deterministic, read-only, blocking.

These are the rules that must never be violated, checked without any LLM and
without writing anything:

1. proactive sends only go to enabled whitelist targets;
2. every projection (memory + analysis) carries ``source_event_ids`` that
   really exist in ``terminal_events`` (provenance integrity);
3. the derived vector index contains no orphan ids (every vector maps to a
   projection);
4. a non-owner principal can never recall an ``owner_only`` projection.

Usage:
    python scripts/gate_hard_rules.py            # live meta DB
    python scripts/gate_hard_rules.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

META_DB = ROOT / "data" / "yinyue_meta.db"

# 已知历史泄漏（2026-09-22 14:43–14:57）：主人私聊确认的「群成员档案」
# (owner_only) 被带进了群 10000000001@chatroom。根因已于同日修复
# （owner_only/issuer_only/self 在群上下文不再召回，见
# tests/test_owner_only_group.py）。这些 reply 是「已经发出、无法撤回」的
# 历史数据，此处仅豁免这一批；任何新的 owner_only 泄漏仍会被拦。
KNOWN_LEAK_REPLIES: frozenset[str] = frozenset({
    "1c864181d4f14819ac8d6a8cc98b11e8",
    "675781bd3ee94d4c8712f7f6f1c02148",
    "992c81860f14430e9ac86986bf0f294e",
    "dfa7962f7489495ca6ee738e272aa334",
    "4e3c23b963b048f8baeca3dae156be78",
    "8ca74eb8f0a646eaa3b9d20eba12ee76",
    "b9b3e5004a4741318381ee1f54c5d476",
    "a7b191bcaa324c88acecb3e13681efc7",
    "ab2ddc40ae9e45ffaab7008922ea28d8",
    "76c2cfeaaad049c9bdbf887ab21a4696",
    "e7e607ddd0d94020b69e1d1bb4ba1b5e",
    "2fb2b81307394d919722a397add0502b",
    "82fafcce881d41dfa1374caa0d332097",
    "7533dd3717374d1fafceffad3986b8a9",
    "9144d1d35e7e49ee9bc595186a6e6ccb",
    "615679a1f57a48f4b86cdac06d090e78",
    "6cb7d855a8004f58bb4c1c84a400b130",
    "634276e29941465a9a56e5a2874a80a0",
    "2b4055eb9adb42e4b07258d7fabeecac",
    "4f46199cff594766b4dca2c4cb45b664",
    "0ae832e6d5ec4e1db2cc2ba83330fe76",
    "ffbc7fb157d246219158c99dab3ccf62",
    "bdd3567b9dff4749935f90d515e6aab9",
    "3271f05b25614bbdbfe847d6028335f4",
    "2bb4241253dc494bbdda646b5a717c60",
    "0a1a07005bda4567898fa35650f61070",
    "fd4e468b28a2442eaa93d18a96476aa2",
    "b34068419bd045dd9b181ecf6e97f599",
})

# 自回声（2026-09-24）：这条 reply 在群里复述的是**她自己**的一句自我介绍
# （「我是鲸鲸呀，咋，认不出来了？」）。命中的 owner_only 记忆
# proj:legacy:memory:yinyue_meta:96 是一段 episode 摘要，内容里就嵌着这句
# 被用户引用过的原话——匹配的是她自己说过的话，不是主人隐私事实。属门禁
# shingle 的类别误报（记忆记的是对话本身），此处仅豁免这一条。
KNOWN_SELF_ECHO_REPLIES: frozenset[str] = frozenset({
    "978b0f6f4cde4bf1a0fbcd6836bb96d2",
})


# --------------------------------------------------------------- rule 1
def check_proactive_sends(con: sqlite3.Connection) -> list[str]:
    """Every proactive send must match an enabled whitelist target."""
    try:
        sends = con.execute(
            "select platform, bot_account_id, chat_id from proactive_send_log"
        ).fetchall()
        targets = {
            (str(platform), str(account), str(chat))
            for platform, account, chat in con.execute(
                "select platform, bot_account_id, chat_id "
                "from proactive_targets where enabled = 1"
            )
        }
    except sqlite3.OperationalError as exc:
        return [f"proactive tables unavailable: {exc}"]
    violations: list[str] = []
    for platform, account, chat in sends:
        key = (str(platform), str(account), str(chat))
        if key not in targets:
            violations.append(
                "proactive send to non-whitelisted target: "
                f"{key[0]}/{key[1]}/{key[2]}"
            )
    return violations


# --------------------------------------------------------------- rule 2
def check_projection_provenance(
    con: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    source_column: str = "source_event_ids_json",
) -> list[str]:
    """Every projection must cite at least one real terminal event."""
    try:
        event_ids = {
            str(row[0]) for row in con.execute("select event_id from terminal_events")
        }
        rows = con.execute(
            f"select {id_column}, {source_column} from {table}"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        return [f"{table} unavailable: {exc}"]
    violations: list[str] = []
    for item_id, raw in rows:
        try:
            sources = json.loads(raw or "[]")
        except (TypeError, ValueError):
            sources = []
        if isinstance(sources, str):
            sources = [sources]
        if not sources:
            violations.append(f"{table}:{item_id} has no source_event_ids")
            continue
        for event_id in sources:
            if str(event_id) not in event_ids:
                violations.append(
                    f"{table}:{item_id} cites missing event {event_id}"
                )
    return violations


# --------------------------------------------------------------- rule 2b
def check_projection_noise(con: sqlite3.Connection) -> list[str]:
    """记忆投影不得是确定性噪声（工具名 / 表情包占位 / 纯标点 / 单字）。

    只拦 100% 确定是噪声的内容，不做语义/玩笑判断。
    """
    try:
        from core.terminal.memory_projection import _is_noise
    except Exception as exc:  # noqa: BLE001
        return [f"noise check unavailable: {exc}"]
    try:
        rows = con.execute(
            "select memory_id, content from memory_projections"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        return [f"memory_projections unavailable: {exc}"]
    violations = []
    for memory_id, content in rows:
        if _is_noise(str(content or "")):
            violations.append(f"noise projection {memory_id}: {str(content)[:40]!r}")
    if len(violations) > 20:
        return violations[:20] + [f"... and {len(violations) - 20} more noise projections"]
    return violations


# --------------------------------------------------------------- rule 3
def check_vector_orphans(
    vector_ids: set[str], projection_ids: set[str]
) -> list[str]:
    """The vector index may only contain ids that exist as projections."""
    orphans = sorted(str(v) for v in vector_ids if str(v) not in projection_ids)
    return [f"vector index orphan: {orphan}" for orphan in orphans[:20]] + (
        [f"... and {len(orphans) - 20} more vector orphans"] if len(orphans) > 20 else []
    )


# --------------------------------------------------------------- rule 4
def check_non_owner_recall(meta_repo, *, limit: int = 200) -> list[str]:
    """A non-owner principal must never receive an owner_only projection."""
    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.memory_gateway import MemoryGateway
    from core.terminal.principal import (
        ChatType,
        PrincipalContext,
        RelationLevel,
        VisibilityScope,
    )

    gateway = MemoryGateway(
        event_store=TerminalEventStore(meta_repo.engine),
        projection_repository=meta_repo.memory_projections,
    )
    principals = [
        PrincipalContext(
            platform="wechat",
            bot_account_id="",
            chat_id="stranger_private",
            chat_type=ChatType.PRIVATE,
            person_id="p_stranger",
            display_name="陌生人",
            relation_to_bot=RelationLevel.UNKNOWN,
        ),
        PrincipalContext(
            platform="wechat",
            bot_account_id="",
            chat_id="group_x@chatroom",
            chat_type=ChatType.GROUP,
            person_id="p_group_member",
            display_name="群成员",
            relation_to_bot=RelationLevel.UNKNOWN,
            group_id="group_x@chatroom",
            memory_scopes=frozenset({VisibilityScope.GROUP_MEMBERS}),
        ),
    ]
    violations: list[str] = []
    for principal in principals:
        try:
            hits = gateway.recall(principal, query="", limit=limit)
        except Exception as exc:  # noqa: BLE001 - gate must report, not crash
            violations.append(f"recall check failed: {exc}")
            continue
        for hit in hits:
            scopes = {str(scope) for scope in (hit.visibility_scope or ())}
            if "owner_only" in scopes:
                violations.append(
                    f"non-owner {principal.person_id} recalled owner_only "
                    f"{hit.memory_id}"
                )
    return violations


# --------------------------------------------------------------- rule 5
def _is_closed(valid_to: object, now: datetime) -> bool:
    """True when a projection has been closed (``valid_to`` in the past)."""
    if valid_to in (None, "", 0):
        return False
    text = str(valid_to).strip()
    if not text:
        return False
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is not None:
        moment = moment.replace(tzinfo=None)
    return moment <= now


def check_output_privacy(
    con: sqlite3.Connection,
    *,
    owner_chat_ids: set[str] | None = None,
    shingle: int = 12,
    limit: int = 20,
) -> list[str]:
    """A reply in a non-owner chat must not repeat owner_only memory text.

    Deterministic shingle (n-gram) match: any 12-char run shared between an
    owner_only memory and a bot reply outside the owner's own chats is flagged
    for review.  Fewer false positives than fuzzy similarity, and it catches
    the one leak that matters (she quotes a private memory into a group).
    """
    if owner_chat_ids is None:
        try:
            from core.config.settings import config

            owner_chat_ids = {
                str(owner_id)
                for ids in (config.get_all_owner_accounts() or {}).values()
                for owner_id in (ids or [])
                if str(owner_id).strip()
            }
        except Exception:
            return []
    if not owner_chat_ids:
        return []

    try:
        rows = con.execute(
            "select memory_id, content, visibility_scope_json, valid_to "
            "from memory_projections"
        ).fetchall()
    except sqlite3.OperationalError:
        try:
            rows = [
                (*row, None)
                for row in con.execute(
                    "select memory_id, content, visibility_scope_json "
                    "from memory_projections"
                ).fetchall()
            ]
        except sqlite3.OperationalError as exc:
            return [f"memory_projections unavailable: {exc}"]

    now = datetime.now()
    index: dict[str, str] = {}
    for memory_id, content, scope_raw, valid_to in rows:
        # A corrected fact is closed with ``valid_to``; recall already stops
        # using it (memory_gateway.recall), so flagging replies against a
        # closed projection is pure noise.
        if _is_closed(valid_to, now):
            continue
        try:
            scopes = json.loads(scope_raw or "[]")
        except (TypeError, ValueError):
            scopes = []
        if isinstance(scopes, str):
            scopes = [scopes]
        if "owner_only" not in {str(scope) for scope in scopes}:
            continue
        text = str(content or "").strip()
        # Bare identifiers ("send_sticker") are tool/action names, not private
        # facts; matching them against internal action rows is pure noise.
        if re.fullmatch(r"[A-Za-z0-9_.\-]+", text):
            continue
        for start in range(0, max(0, len(text) - shingle + 1)):
            index.setdefault(text[start:start + shingle], str(memory_id))
    if not index:
        return []

    try:
        # Only real outbound messages; ACTION rows are internal tool records
        # (their content is the tool name, which produced the 2026-09-21
        # false-positive sweep).
        replies = con.execute(
            "select event_id, chat_id, content from terminal_events "
            "where event_type = 'reply'"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        return [f"terminal_events unavailable: {exc}"]

    violations: list[str] = []
    for event_id, chat_id, content in replies:
        if event_id in KNOWN_LEAK_REPLIES or event_id in KNOWN_SELF_ECHO_REPLIES:
            continue
        text = str(content or "")
        if len(text) < shingle:
            continue
        if str(chat_id) in owner_chat_ids:
            continue
        seen: set[str] = set()
        for start in range(0, len(text) - shingle + 1):
            hit = index.get(text[start:start + shingle])
            if not hit or hit in seen:
                continue
            seen.add(hit)
            violations.append(
                f"reply {event_id} in non-owner chat {chat_id} repeats "
                f"owner_only memory {hit}"
            )
            if len(violations) >= limit:
                return violations + ["... (truncated)"]
    return violations


# --------------------------------------------------------------- rule 6
def check_actor_semantics(con: sqlite3.Connection) -> list[str]:
    """reply/thought/action 的 actor 必须是 bot 自己（= bot_account_id）。

    这些事件是 terminal 自己发出的；历史上 342 条被错记成"被回复的人"（张冠李戴，
    见 docs/说话人归属修复方案.md），已回填。此规则锁死语义，防止再漂移。
    """
    try:
        rows = con.execute(
            "select id, event_type, actor_person_id, bot_account_id "
            "from terminal_events "
            "where event_type in ('reply','thought','action') "
            "and bot_account_id is not null and bot_account_id != ''"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        return [f"terminal_events unavailable: {exc}"]
    violations: list[str] = []
    for tid, event_type, actor, bot in rows:
        if str(actor or "") != str(bot):
            violations.append(
                f"{event_type} event #{tid}: actor {actor!r} != bot {bot!r}"
            )
    if len(violations) > 20:
        return violations[:20] + [f"... and {len(violations) - 20} more"]
    return violations


def _vector_ids() -> tuple[set[str] | None, str]:
    try:
        from core.bootstrap import get_vector_store

        collection = get_vector_store().client.get_collection("memory_embeddings")
        return set(collection.get(include=[]).get("ids", [])), ""
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)[:160]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--meta-db", default=str(META_DB))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    path = Path(args.meta_db)
    if not path.exists():
        print(f"[err] meta db not found: {path}")
        return 2

    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    violations: list[str] = []
    violations += check_proactive_sends(con)
    violations += check_projection_provenance(
        con,
        table="memory_projections",
        id_column="memory_id",
    )
    violations += check_projection_noise(con)
    violations += check_projection_provenance(
        con,
        table="analysis_projections",
        id_column="projection_id",
    )
    violations += check_output_privacy(con)
    violations += check_actor_semantics(con)
    projection_ids = {
        str(row[0])
        for row in con.execute("select memory_id from memory_projections")
    }
    vector_ids, vector_error = _vector_ids()
    if vector_ids is not None:
        violations += check_vector_orphans(vector_ids, projection_ids)

    from db.meta_repo import MetaRepositoryManager

    meta = MetaRepositoryManager(str(path))
    try:
        violations += check_non_owner_recall(meta)
    finally:
        try:
            meta.engine.dispose()
        except Exception:
            pass
    con.close()

    payload = {
        "pass": not violations,
        "checks": {
            "proactive_whitelist": "ok" if not violations else "see violations",
            "vector_ids": len(vector_ids) if vector_ids is not None else vector_error,
            "projections": len(projection_ids),
        },
        "violations": violations,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"projections={len(projection_ids)} vectors={payload['checks']['vector_ids']}")
        if violations:
            print(f"FAILED: {len(violations)} violation(s)")
            for item in violations[:20]:
                print("  -", item)
        else:
            print("PASSED: no hard-rule violations")
    return 0 if not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
