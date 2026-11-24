"""群成员名单同步：把主人确认的名单写成 person_notes **逐人卡**。

主人私聊确认新版名单后，由 process_pending_roster_confirms 自动落库；
也可用 scripts/sync_group_roster.py 手动同步（dry-run 默认）。

设计沿革：
* 2026-09-23：群成员身份 = person_notes（唯一真相源），不再往 memory_projections
  写 group_members 逐人条目 / owner_only 准版（那会造成双份冗余）。
* **2026-09-24：落点从「群级备注」改为「逐人卡」**。群级备注（``person_key=''``）
  的契约是「群基线 / 她的立场」，不是名册——旧版把整份名册写进去，导致
  ``[场外补充]`` 每行硬截 90 字吃掉后半截，并且与 ``[群成员名单·权威]``、
  ``[在场人·备注]`` 三处重复渲染同一份身份。现在：名册只进逐人卡（按 wxid），
  名单块一人一行完整渲染一次；群级备注不再被名册污染。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

import structlog
from safelog import warn

log = structlog.get_logger()

_CLASS_RE = re.compile(
    r"(?:计算机|物联网|软件|网络|电子|通信|数学|物理|化学|英语|会计|金融)?\d{1,2}-\d{1,2}班"
    r"|[\u4e00-\u9fff]{2,6}专业"
)
_ROLE_RE = re.compile(
    r"(?:前|现任|原|现)?(?:学委|学习委员|班长|副班长|团支书|心理委员|生活委员"
    r"|体育委员|文艺委员|宣传委员|组织委员|纪律委员|信息员)"
)


def parse_roster(text: str) -> list[dict[str, str]]:
    """Parse confirmed roster lines -> ``[{nick, class, role, full}]``."""
    members: list[dict[str, str]] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip().lstrip("-•*· ").strip()
        if not line or line.startswith("[") or "成员清单" in line:
            continue
        if line.startswith(("（", "(")) or line.startswith("附注") or line.startswith("全群"):
            continue
        first = re.split(r"[，,]", line)[0]
        nick = re.sub(r"（[^）]*）|\([^)]*\)", "", first).strip()
        if not nick:
            continue
        class_match = _CLASS_RE.search(line)
        role_match = _ROLE_RE.search(line)
        role = role_match.group(0) if role_match else ""
        if role and "现任" in line and "现任" not in role:
            role = f"{role}（现任）"
        class_text = class_match.group(0) if class_match else ""
        desc = ""
        if not role:
            for index, part in enumerate(re.split(r"[，,]", line)):
                if index == 0:
                    continue
                clean = re.sub(r"（[^）]*）|\([^)]*\)", "", part).strip()
                if not clean or clean in ("男", "女") or clean == class_text:
                    continue
                if re.fullmatch(r"[\u4e00-\u9fff]{2,6}", clean):
                    desc = clean
                    break
        members.append(
            {
                "nick": nick,
                "class": class_text,
                "role": role,
                "desc": desc,
                "full": line,
            }
        )
    return members


def parse_notes(text: str) -> list[str]:
    """Non-member notes (e.g. "全群在任学委只有 X 一人") -> canonical tail."""
    notes: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip().lstrip("-•*· ").strip()
        if line.startswith(("全群", "附注")) and len(line) <= 80:
            notes.append(line.rstrip("。"))
    return notes


def guess_group_chat_id(meta_repo: Any, nicknames: Iterable[str], *, conn: Any = None) -> str:
    """Pick the group whose member nicknames best overlap the roster."""
    wanted = {str(n).strip() for n in nicknames if str(n).strip()}
    if not wanted:
        return ""
    try:
        from sqlalchemy import text as _sa_text

        statement = _sa_text("select chat_id, group_nickname from group_memberships")
        if conn is not None:
            rows = conn.execute(statement).fetchall()
        else:
            with meta_repo.engine.connect() as own:
                rows = own.execute(statement).fetchall()
    except Exception:
        warn("core/quality/roster_sync.py:96 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""
    by_chat: dict[str, set[str]] = {}
    for chat_id, nick in rows:
        by_chat.setdefault(str(chat_id or ""), set()).add(str(nick or "").strip())
    best, best_hits = "", 0
    for chat_id, names in by_chat.items():
        hits = len(wanted & names)
        if hits > best_hits:
            best, best_hits = chat_id, hits
    threshold = max(2, (len(wanted) + 1) // 2)
    return best if best_hits >= threshold else ""


def sync_roster(
    meta_repo: Any,
    *,
    roster_text: str,
    chat_id: str = "",
    owner_chat_id: str = "",
    platform: str = "wechat",
    bot_account_id: str = "",
    actor_person_id: str = "",
    reason: str = "主人确认名单",
    apply: bool = True,
    conn: Any = None,
) -> dict:
    """把主人确认的群成员名单写成 person_notes **逐人卡**（person_key=wxid）。"""
    members = parse_roster(roster_text)
    result: dict[str, Any] = {
        "members": len(members),
        "chat_id": chat_id,
        "dry_run": not apply,
    }
    if not members:
        result["error"] = "no members parsed"
        return result
    if not chat_id:
        chat_id = guess_group_chat_id(meta_repo, [m["nick"] for m in members], conn=conn)
        result["chat_id"] = chat_id
    if not chat_id:
        result["error"] = "cannot locate group"
        return result

    # 2026-09-24：名册落点 = 逐人卡。群级备注留给「群基线/立场」（见模块 docstring）。
    # 也彻底停掉往 memory_projections 写 group_members 条目（2026-09-23 已停）。
    from core.context.person_notes import upsert_note

    wxid_by_nick = _nick_wxid_map(meta_repo, chat_id, conn=conn)
    written: list[str] = []
    unresolved: list[str] = []
    for member in members:
        nick = str(member.get("nick") or "").strip()
        wxid = wxid_by_nick.get(nick, "")
        if not wxid:
            unresolved.append(nick)
            continue
        if not apply:
            written.append(nick)
            continue
        upsert_note(
            person_key=wxid,
            content=str(member.get("full") or "").strip(),
            chat_id=chat_id,
            platform=platform,
            account_id=bot_account_id,
            source="owner_confirmed",
            meta_repo=meta_repo,
            conn=conn,
        )
        written.append(nick)
    # 非成员行（如「全群在任学委只有 X 一人」）不再往群级备注塞；显式回报，
    # 免得主人的内容被静默丢掉（调用方会把 result 写进 roster_sync_done 日志）。
    result["person_notes_written"] = len(written)
    result["unresolved_members"] = unresolved
    result["notes_not_persisted"] = parse_notes(roster_text)
    if not written:
        result["error"] = "no member matched a wxid"
    return result


def _nick_wxid_map(meta_repo: Any, chat_id: str, *, conn: Any = None) -> dict[str, str]:
    """``{群昵称: wxid}``（逐人卡的 person_key 就是 wxid）。"""
    try:
        from sqlalchemy import text as _sa_text

        statement = _sa_text(
            "select gm.group_nickname, pi.identity_value "
            "from group_memberships gm "
            "left join person_identities pi "
            "  on pi.person_id = gm.person_id "
            " and pi.platform = gm.platform "
            " and pi.identity_type = 'wxid' "
            "where gm.chat_id = :chat"
        )
        if conn is not None:
            rows = conn.execute(statement, {"chat": str(chat_id)}).fetchall()
        else:
            with meta_repo.engine.connect() as own:
                rows = own.execute(statement, {"chat": str(chat_id)}).fetchall()
    except Exception:
        warn("core/quality/roster_sync.py:193 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}
    out: dict[str, str] = {}
    for nick, wxid in rows:
        name, key = str(nick or "").strip(), str(wxid or "").strip()
        if name and key and key != "unknown":
            out.setdefault(name, key)
    return out


_ROSTER_SCAN_FACTOR = 8
_ROSTER_SCAN_MIN = 32


def _roster_quote(metadata_json: Any) -> str:
    try:
        meta = json.loads(str(metadata_json or "") or "{}")
    except (TypeError, ValueError) as exc:
        log.warning("roster_metadata_invalid", error=repr(exc))
        return ""
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("quote_content") or "").strip()


def _set_roster_status(claim, repository, status: str) -> bool:
    """名单确认不写投影，因此终态 CAS 不套 memory_projections 停写闸。"""
    from sqlalchemy.orm import Session

    from core.memory.feedback_commit import check_feedback, FeedbackConflict

    with Session(repository.write_engine()) as session:
        connection = session.connection()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        row = check_feedback(session, claim, repository.read_engine())
        if row.status == status:
            return False
        if row.status != "new":
            raise FeedbackConflict("roster_feedback_state_changed")
        row.status = status
        session.commit()
        return True


def _consume_roster(claim, *, meta_repo: Any, quote: str, platform: str, account_id: str, chat_id: str, sender: str) -> str:
    """写逐人卡 + SYSTEM 收据 + sample learned，同一事务提交。"""
    from sqlalchemy.orm import Session

    from core.memory.feedback_commit import check_feedback, matching_commit, FeedbackConflict
    from core.terminal.event_model import Event, EventType
    from core.terminal.event_store_db import append_in_session
    from core.terminal.principal import VisibilityScope

    engine = meta_repo.feedback_samples.write_engine()
    with Session(engine) as session:
        connection = session.connection()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        row = check_feedback(session, claim, engine)
        if row.status == "learned":
            if matching_commit(session, claim, event_type="system", source="roster_confirm"):
                return "recovered"
            raise FeedbackConflict("roster_receipt_missing")
        if row.status != "new":
            raise FeedbackConflict("roster_feedback_state_changed")
        if matching_commit(session, claim, event_type="system", source="roster_confirm"):
            raise FeedbackConflict("roster_receipt_without_consumption")
        outcome = sync_roster(
            meta_repo,
            roster_text=quote,
            owner_chat_id=chat_id,
            platform=platform,
            bot_account_id=account_id,
            actor_person_id=sender,
            reason="主人私聊确认名单（自动）",
            conn=connection,
        )
        if outcome.get("error"):
            row.status = "reviewed"
            session.commit()
            return "reviewed"
        receipt = Event.new(
            event_id=claim.event_id,
            occurred_at=claim.created_at,
            event_type=EventType.SYSTEM,
            platform=platform,
            bot_account_id=account_id,
            chat_id=chat_id,
            chat_type="private",
            actor_person_id=sender,
            content="[feedback] roster_confirm consumed",
            visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
            metadata={"type": "feedback_consumption", "source": "roster_confirm", "feedback": claim.binding},
        )
        append_in_session(session, receipt)
        row.status = "learned"
        session.commit()
        return "synced"


async def process_pending_roster_confirms(
    *,
    limit: int = 3,
    meta_repo: Any = None,
) -> dict:
    """主人确认的名单消息 -> 逐人卡；按源版本 CAS，效果与 learned 收据同事务。

    积压按最老优先：旧确认不会被连续新确认饿死；同一批最多做 ``limit`` 次
    终态消费，但允许扫过限流/冲突样本。
    """
    stats = {"scanned": 0, "synced": 0, "skipped": 0, "reviewed": 0, "recovered": 0, "conflicts": 0, "rate_limited": 0}
    try:
        if meta_repo is None:
            from core.terminal.store_port import get_meta_repo

            meta_repo = get_meta_repo()
    except Exception:
        warn("core/quality/roster_sync.py:215 Exception", err=repr(__import__("sys").exc_info()[1]))
        return stats
    if meta_repo is None:
        return stats
    try:
        from core.quality.corrections import _owner_ids, _rate_ok
    except Exception:
        warn("core/quality/roster_sync.py:221 Exception", err=repr(__import__("sys").exc_info()[1]))
        return stats

    from core.memory.feedback_commit import claim_feedback, recover_feedback, FeedbackConflict

    budget = max(1, int(limit))
    window = max(budget * _ROSTER_SCAN_FACTOR, _ROSTER_SCAN_MIN)
    samples = meta_repo.feedback_samples.list_pending(limit=window, source="roster_confirm")
    spent = 0
    for sample in samples:
        if spent >= budget:
            break
        stats["scanned"] += 1
        try:
            claim = claim_feedback(sample, meta_repo.feedback_samples, source="roster_confirm", namespace="roster-confirm")
            platform, account_id, chat_id, sender = claim.identity
            if sender not in _owner_ids(platform):
                if _set_roster_status(claim, meta_repo.feedback_samples, "skipped_non_owner"):
                    stats["skipped"] += 1
                spent += 1
                continue
            if not _rate_ok((platform, account_id, chat_id)):
                stats["rate_limited"] += 1
                continue
            if recover_feedback(claim, meta_repo.feedback_samples, event_type="system", source="roster_confirm"):
                stats["recovered"] += 1
                spent += 1
                continue
            quote = _roster_quote(sample.metadata_json)
            if not quote or ("成员清单" not in quote and "唯一准版" not in quote):
                if _set_roster_status(claim, meta_repo.feedback_samples, "reviewed"):
                    stats["reviewed"] += 1
                spent += 1
                continue
            outcome = _consume_roster(
                claim,
                meta_repo=meta_repo,
                quote=quote,
                platform=platform,
                account_id=account_id,
                chat_id=chat_id,
                sender=sender,
            )
        except FeedbackConflict as exc:
            stats["conflicts"] += 1
            log.warning("roster_confirm_source_conflict", sample=sample.id, reason=str(exc))
            continue
        stats[outcome] += 1
        if outcome != "rate_limited":
            spent += 1
    return stats
