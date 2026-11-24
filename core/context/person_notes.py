"""Person notes：``[场外补充]`` 块的渲染 + ``person_notes`` 表的读写。

    [场外补充]
    - 这个群日常就是互相拆台、玩梗，别当真去执行。

**槽位契约（2026-09-24 明确）**：``person_key=''`` 的群级备注只装「群基线 / 她的
立场」这类原文里看不出来的半长期背景，**不装成员名册**。成员身份的唯一落点是
``person_key=wxid`` 的逐人卡，由 ``agents/conversational.py`` 的
``[群成员名单·权威]`` 块**一人一行、完整渲染一次**。

历史：原先还有一个 ``build_person_notes_block`` 渲染 ``[在场人·备注]``（在场人
≤3 人 + 活跃统计）。它与名单块共用同一批逐人卡、身份被渲染两次（且名单块只取
「；」前段，边界话只在前者），2026-09-24 整块删除、活跃统计一并砍掉。

Read-only, no LLM.  Any failure degrades to "" so the prompt never breaks.
登记：agents/conversational.py 构建 [场外补充]；开关 YINYUE_FEATURE_PERSON_NOTES；
读写走 scripts/person_notes.py。
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any
from safelog import warn
from core.infra.envflags import flag, text

NOTE_MAX = 90

# 词条行（`词=释义`）：词 ≤8 字、整行只一个「=」；群基线/立场行不算词条（M3 注入口径）
_LEXICON_PART_RE = re.compile(r"^([^=；;]{1,8})=([^=]+)$")


def is_lexicon_part(part: str) -> bool:
    """是不是一条词条行（`词=释义`，词 ≤8 字、只一个「=」）。"""
    return bool(_LEXICON_PART_RE.match(str(part or "").strip()))


def lexicon_term(part: str) -> str:
    """取词条行里的「词」；不是词条行则返回 ""。"""
    match = _LEXICON_PART_RE.match(str(part or "").strip())
    return match.group(1).strip() if match else ""


def _flag_on() -> bool:
    return flag("YINYUE_FEATURE_PERSON_NOTES")


def _chats() -> set[str]:
    raw = text("YINYUE_PERSON_NOTES_CHATS")
    if not raw:
        raw = text("YINYUE_CHAT_WINDOW_CHATS")
    return {part.strip() for part in raw.split(",") if part.strip()}


def notes_enabled_for(chat_id: str) -> bool:
    """Flag on, and (list empty ⇒ all chats / list set ⇒ only those chats)."""
    if not _flag_on():
        return False
    chats = _chats()
    if not chats:
        return True
    return str(chat_id or "") in chats


def _flat(text: str, limit: int = NOTE_MAX) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def build_chat_context_block(
    chat_id: str,
    *,
    platform: str = "",
    account_id: str = "",
    lines: list[str] | None = None,
    max_lines: int = 4,
    max_chars: int = 300,
    focus_text: str = "",
) -> str:
    """`[场外补充]` 块：这个群/这段关系的**半长期**背景（docs §2 第③项）。

    内容来源是 `person_notes` 里 `person_key=''` 的"群级备注"（主人手写或提案批准）：
    群基线（"日常拆台、玩笑为主"）、她的立场（"拒过换皮咒语"）这类
    **原文里看不出来的东西**；"此刻在聊什么"由窗口④的原文承担，这里不重复。

    ``lines`` 供测试注入。任何失败都退化成 ""。
    """
    try:
        rows = lines
        if rows is None:
            rows = load_chat_notes(chat_id, platform=platform, account_id=account_id)
        out: list[str] = []
        used = 0
        for row in rows:
            # 一条群级备注可以是多行（用换行或 | 分隔），逐行渲染
            for raw in str(row).replace("|", "\n").splitlines():
                part = str(raw).strip()
                # M3 注入口径：词条行「命中才注入」——当前消息/近期群里没提到该词条就不塞进去；
                # 群基线/她的立场（非词条行）照旧常驻。focus_text 为空 = 旧行为（兼容测试/老调用）。
                if focus_text and is_lexicon_part(part):
                    term = lexicon_term(part)
                    if term and term.casefold() not in str(focus_text).casefold():
                        continue
                body = _flat(part, 90)
                if not body or len(out) >= max(1, int(max_lines)):
                    continue
                line = f"- {body}"
                if used + len(line) + 1 > max_chars:
                    break
                out.append(line)
                used += len(line) + 1
        if not out:
            return ""
        return "[场外补充]\n" + "\n".join(out)
    except Exception:
        warn("core/context/person_notes.py:116 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def load_chat_notes(
    chat_id: str, *, platform: str = "", account_id: str = ""
) -> list[str]:
    """读群级备注（person_key='' 的行），按更新时间。"""
    try:
        from core.terminal.store_port import get_meta_repo
        from sqlalchemy import text as _sql

        with get_meta_repo().engine.connect() as conn:
            rows = conn.execute(
                _sql(
                    "select content from person_notes "
                    "where active = 1 and chat_id = :chat and person_key = '' "
                    "and (platform = '' or platform = :platform) "
                    "order by id"
                ),
                {"chat": str(chat_id), "platform": str(platform or "")},
            ).mappings().all()
        return [str(r["content"] or "") for r in rows if str(r["content"] or "").strip()]
    except Exception:
        warn("core/context/person_notes.py:139 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []
def upsert_note(
    *,
    person_key: str,
    content: str,
    chat_id: str = "",
    platform: str = "",
    account_id: str = "",
    source: str = "owner",
    source_proposal_id: int = 0,
    evidence_json: str = "",
    clear_provenance: bool = False,
    meta_repo: Any = None,
    conn: Any = None,
) -> bool:
    """Create/update one note (used by scripts/person_notes.py).

    溯源（2026-09-29 P2 收口）：批准路径传 ``source_proposal_id`` /
    ``evidence_json``；普通写入不传时保留原值；撤回传 ``clear_provenance=True``。
    ``conn`` 供调用方把多条 note 与别的状态放进**同一个事务**；传了由调用方
    commit/rollback，本函数不再自开事务。
    """
    from sqlalchemy import text as _sql

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    statement = _sql(
        "insert into person_notes "
        "(platform, account_id, person_key, chat_id, content, source, "
        " source_proposal_id, evidence_json, active, created_at, updated_at) "
        "values (:platform, :account_id, :person_key, :chat_id, :content, :source, "
        " :source_proposal_id, :evidence_json, 1, :now, :now) "
        "on conflict(platform, account_id, person_key, chat_id) do update set "
        "content = excluded.content, source = excluded.source, "
        "source_proposal_id = case "
        "  when :clear_provenance = 1 then 0 "
        "  when excluded.source_proposal_id > 0 then excluded.source_proposal_id "
        "  else person_notes.source_proposal_id end, "
        "evidence_json = case "
        "  when :clear_provenance = 1 then '' "
        "  when excluded.evidence_json != '' then excluded.evidence_json "
        "  else person_notes.evidence_json end, "
        "active = 1, updated_at = excluded.updated_at"
    )
    values = {
        "platform": str(platform or ""),
        "account_id": str(account_id or ""),
        "person_key": str(person_key),
        "chat_id": str(chat_id or ""),
        "content": str(content or ""),
        "source": str(source or "owner"),
        "source_proposal_id": int(source_proposal_id or 0),
        "evidence_json": str(evidence_json or ""),
        "clear_provenance": 1 if clear_provenance else 0,
        "now": now,
    }
    if conn is not None:
        conn.execute(statement, values)
        return True
    if meta_repo is None:
        from core.terminal.store_port import get_meta_repo

        meta_repo = get_meta_repo()
    with meta_repo.engine.begin() as own:
        own.execute(statement, values)
    return True


def set_active(
    *,
    person_key: str,
    chat_id: str = "",
    platform: str = "",
    account_id: str = "",
    active: bool = False,
) -> int:
    """Enable/disable notes (soft delete, reversible)."""
    from sqlalchemy import text as _sql

    from core.terminal.store_port import get_meta_repo

    with get_meta_repo().engine.begin() as conn:
        result = conn.execute(
            _sql(
                "update person_notes set active = :active, updated_at = :now "
                "where person_key = :person_key and chat_id = :chat_id "
                "and platform = :platform and account_id = :account_id"
            ),
            {
                "active": 1 if active else 0,
                "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "person_key": str(person_key),
                "chat_id": str(chat_id or ""),
                "platform": str(platform or ""),
                "account_id": str(account_id or ""),
            },
        )
    return int(result.rowcount or 0)


def list_notes(chat_id: str = "", active_only: bool = True) -> list[dict]:
    from sqlalchemy import text as _sql

    from core.terminal.store_port import get_meta_repo

    sql = "select person_key, chat_id, platform, account_id, content, source, source_proposal_id, evidence_json, active, updated_at from person_notes"
    clauses = []
    params: dict[str, object] = {}
    if chat_id:
        clauses.append("(chat_id = :chat or chat_id = '')")
        params["chat"] = str(chat_id)
    if active_only:
        clauses.append("active = 1")
    if clauses:
        sql += " where " + " and ".join(clauses)
    sql += " order by chat_id, person_key"
    with get_meta_repo().engine.connect() as conn:
        return [dict(r) for r in conn.execute(_sql(sql), params).mappings()]
