"""Memory proposals: the staged ("propose, don't write") path (P2).

docs/记忆的定义.md §3:

* only five structured slots are allowed (appointment / open_loop / identity /
  preference / habit) — no free-form notes;
* nothing takes effect until the owner approves it; candidates live in
  ``memory_proposals`` with their evidence event ids and the reason signal;
* approval writes a ``person_notes`` row (the same block the prompt already
  renders), so an approved item shows up in her next turn;
* per-subject cap keeps the note pool from growing without bound.

The extractor is one small LLM call with a strict JSON contract; everything else
is deterministic.  Any failure degrades to "no proposals".

归属：scheduler/main.py::run_memory_proposals（每天≤1 次，只写 pending）+ scripts/memory_proposals.py 审阅；批准才写 person_notes；设计 docs/记忆的定义.md §3。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from typing import Any

import structlog
from safelog import warn
from core.infra.envflags import flag

log = structlog.get_logger()

SLOTS: dict[str, str] = {
    "appointment": "约定",
    "open_loop": "未了结",
    "identity": "身份",
    "preference": "偏好",
    "habit": "习惯",
    # 批 3b 上沉：群内术语/黑话的释义（L4 反复命中 → L1 词典）
    "lexicon": "术语",
}
REASONS = {"open_loop", "repeat", "emotion", "owner", "llm"}
MAX_PER_SUBJECT = 5
MAX_CONTENT = 90

# 批 3c：v2 只提「悬而未决 + 重复模式」。身份类不进提案（名单/群级备注才是
# 身份真相源，重复提案只会制造冗余——09-22 那批 6 条里 4 条就栽在这）。
SLOTS_V2: dict[str, str] = {
    "open_loop": "未了结",
    "habit": "习惯",
}
# 「重复模式」的统计门槛：同一模式至少出现这么多次才算 pattern
REPEAT_MIN_OCCURRENCES = 3

# ------------------------------------------------------------------ prompts
_SYSTEM_PROMPT_V1 = (
    "你在给一个聊天助手做「记忆沉淀」提案。给你一段真实的群聊/私聊记录，"
    "挑出**将来还会被用到**的东西，只能落进下面五类槽位之一：\n"
    "  appointment（约定与承诺，例：下周三给你看成绩单）\n"
    "  open_loop（未了结事项，例：等 P3 确认学委名单）\n"
    "  identity（身份与关系，例：P6是物联网25-2班的）\n"
    "  preference（偏好与忌讳，例：主人不吃香菜）\n"
    "  habit（反复出现的行为模式，例：Y-K 爱起哄、自封第一）\n"
    "只输出 JSON：\n"
    '{"proposals":[{"slot":"identity","subject":"Y-K","content":"一句话，脱离上下文也读得懂",'
    '"evidence":["事件id"],"reason":"open_loop|repeat|emotion|owner"}]}\n'
    "规则（宁缺毋滥）：\n"
    "- 玩笑、自称、@、表情、工具名、她自己的回话，一律不要；\n"
    "- 身份只写群名单里没有的细节（班级/职务/关系/本名）；「X 是本群成员」这类不要提；\n"
    "- **习惯只写「他怎么说话/什么脾气」，绝不写「他要求什么」**；\n"
    "  玩笑里的要求与命令（例：「不准再改榜单」「必须叫我第一」）是坑，会让她拿它当规则拒绝配合，一律不要；\n"
    "- content 必须脱离当时对话也能读懂，一句话，≤60 字，不要改写原意；\n"
    "- subject 写这个槽位挂在谁身上（用记录里的昵称）；整段对话共有的可以留空；\n"
    "- evidence 只能填记录里出现过的 id，不能编造；\n"
    "- 没有值得沉淀的就返回 {\"proposals\":[]}。"
)

# 批 3c 收紧：只留两类，且明确把身份类挡在外面（名单已有）。
_SYSTEM_PROMPT_V2 = (
    "你在给一个聊天助手做「记忆沉淀」提案。给你一段真实的群聊记录，"
    "**只挑两类**将来还会被用到的东西：\n"
    "  open_loop（悬而未决：还没了结的约定 / 待办 / 等回音的事。"
    "例：等 P3 确认学委名单；答应了周三给你看成绩单）\n"
    "  habit（重复出现的模式：**至少在本段记录里出现过 3 次**的行为特征。"
    "例：Y-K 反复自封「牛子榜第一」；某人每次都先阴阳一句再说话）\n"
    "只输出 JSON：\n"
    '{"proposals":[{"slot":"open_loop","subject":"P8",'
    '"content":"一句话，脱离上下文也读得懂","evidence":["事件id"],'
    '"evidence_text":"最能代表它的原话片段（≤30字）","reason":"open_loop|repeat"}]}\n'
    "硬规则（宁缺毋滥，宁可返回空）：\n"
    "- **只写记录里显式出现的目标**：记录行写成「谁 → @对谁: 内容」时才用那个\n"
    "  目标；没写目标（只有「谁: 内容」）就写「某人」，**不许从内容猜**\n"
    "  （例：不能因为某人恰好在附近说话，就把「对谁说」按到他头上）；\n"
    "- **身份类一律不要提**：谁是哪班的、谁是什么职位、谁是本群成员——"
    "名单和群级备注里已经有了，提了就是重复；\n"
    "- **不要提她自己**（她的习惯、她说的话、她爱做什么）——那是回声，不是对人的认识；\n"
    "- **习惯只写「他怎么说话/什么脾气」，绝不写「他要求什么」**。"
    "玩笑里的要求与命令（「不准再改榜单」「必须叫我第一」）是坑，"
    "会让她拿它当规则拒绝配合，一律不要；\n"
    "- habit 必须有 ≥3 次重复的证据，只出现一次的不是模式；\n"
    "- content 必须脱离当时对话也能读懂，一句话，≤60 字，不要改写原意；\n"
    "- evidence 只能填记录里出现过的 id，不能编造；evidence_text 必须是原文片段；\n"
    "- 没有值得沉淀的就返回 {\"proposals\":[]}。"
)


#: 回合内便签（R3）：判这一轮直接产 0~3 条候选便签。
#: ``off``（默认）不写、不问；``shadow`` 只在日志里记账；``on`` 写进候选区。
MEMORY_NOTES_ENV = "YINYUE_FEATURE_MEMORY_TURN_NOTES"


def memory_notes_mode() -> str:
    """三档：off / shadow / on（读不到或写错一律当 off）。"""
    raw = str(os.environ.get(MEMORY_NOTES_ENV) or "").strip().lower()
    if raw in ("on", "1", "true", "yes"):
        return "on"
    if raw == "shadow":
        return "shadow"
    return "off"


def memory_notes_enabled() -> bool:
    """问不问那一问 / 要不要处理读数（off 时 prompt 与旧实现逐字节相同）。"""
    return memory_notes_mode() != "off"


def stage_from_turn(
    chat_id: str,
    raw: list,
    *,
    valid_ids: set[str],
    names: dict[str, str] | None = None,
    dry_run: bool = False,
) -> dict:
    """把判这一轮给的便签**直接**送进候选区（不跑抽取调用、不写常驻记忆）。

    与 ``propose_from_chat`` 的区别只有一处：条目来源从"再调一次 LLM 从事件里抽"
    变成"判在同一个回合里已经写好的 0~3 条"。校验 / 去重 / 上限 / 只写 pending
    全部复用，一行不改（``memory_proposals`` 仍是候选区的唯一写入口）。
    """
    items = [dict(item) for item in (raw or ()) if isinstance(item, dict)]
    if not items:
        return {"seen": 0, "staged": 0, "dry_run": bool(dry_run)}
    if dry_run:
        checked = [c for c in (_validate_item(i, set(valid_ids)) for i in items) if c]
        return {"seen": len(items), "staged": 0, "valid": len(checked), "dry_run": True}
    staged = _stage(chat_id, items, set(valid_ids), dict(names or {}))
    return {"seen": len(items), "staged": int(staged), "dry_run": False}


def memory_note_slots() -> dict[str, str]:
    """判本轮可以提哪些槽位（跟着 v2 开关走）。

    提示词与 ``_validate_item`` 必须读**同一份**：各写一份的结果是「模型照着提示词
    提了 preference，校验按 v2 把它丢了」，用户看到的是「说了记一下却没进候选区」。
    """
    return dict(SLOTS_V2) if proposals_v2_enabled() else dict(SLOTS)


def proposals_v2_enabled() -> bool:
    """批 3c 开关：只提悬而未决 + 重复模式（默认关 = 旧五槽位行为）。"""
    return flag("YINYUE_FEATURE_MEMORY_PROPOSALS_V2")


def _system_prompt() -> str:
    return _SYSTEM_PROMPT_V2 if proposals_v2_enabled() else _SYSTEM_PROMPT_V1


# 习惯槽里出现这些词 = 把玩笑里的"要求"固化成了规则（主人 2026-09-22 点名的有害记忆）
_INSTRUCTION_MARKERS = ("不准", "不许", "必须", "禁止", "以后都", "谁再", "一定要")
# 说她自己的"习惯"= 回声，不是对人的认识（主人 2026-09-22 要求驳回 #4）
_BOT_SUBJECTS = ("鲸鲸", "她", "自己", "助手")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _flat(text: Any, limit: int = MAX_CONTENT) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


# --------------------------------------------------------------------- extract
def _load_events(conn, chat_id: str, hours: int, limit: int) -> list[dict]:
    from sqlalchemy import text as _sql

    since = datetime.now() - timedelta(hours=max(1, int(hours)))
    rows = conn.execute(
        _sql(
            "select event_id, occurred_at, actor_person_id, event_type, content, "
            "parent_event_id, recipient_person_ids_json, metadata_json "
            "from terminal_events where chat_id = :chat and occurred_at >= :since "
            "and event_type in ('message','question','reply','request') "
            "order by occurred_at desc limit :limit"
        ),
        {"chat": str(chat_id), "since": since, "limit": max(5, int(limit))},
    ).mappings().all()
    out = [dict(r) for r in rows]
    out.reverse()  # 正序留给 LLM
    return out


def _speakers(conn) -> dict[str, str]:
    from sqlalchemy import text as _sql

    out: dict[str, str] = {}
    for row in conn.execute(
        _sql(
            "select pi.identity_value, pe.display_name from person_identities pi "
            "join person_entities pe on pe.person_id = pi.person_id "
            "where pe.display_name is not null and pe.display_name != ''"
        )
    ).mappings():
        out.setdefault(str(row["identity_value"]), str(row["display_name"]))
    return out


def _identity_index(conn) -> tuple[dict[str, str], dict[str, str]]:
    """``(identity→person_id, id/identity→display_name)``（归属判定用）。"""
    from sqlalchemy import text as _sql

    identities: dict[str, str] = {}
    display_names: dict[str, str] = {}
    for row in conn.execute(
        _sql(
            "select pi.identity_value, pi.person_id, pe.display_name "
            "from person_identities pi "
            "left join person_entities pe on pe.person_id = pi.person_id"
        )
    ).mappings():
        value = str(row["identity_value"] or "")
        person_id = str(row["person_id"] or "")
        name = str(row["display_name"] or "")
        if value and person_id:
            identities.setdefault(value, person_id)
        if name:
            if value:
                display_names.setdefault(value, name)
            if person_id:
                display_names.setdefault(person_id, name)
    return identities, display_names


def _render_events(
    events: list[dict],
    names: dict[str, str],
    *,
    identities: dict[str, str] | None = None,
    display_names: dict[str, str] | None = None,
) -> tuple[str, set[str]]:
    """记录行 → ``[id] 谁 → 对谁: 内容``（归属走 canonical，不猜）。

    2026-09-30 收口：原来只写 ``[id] 谁: 内容``，"对谁说"整个丢了——抽取模型
    于是只能从内容猜目标（"这句你刚对P7说过"那类事故的温床）。现在每条都带
    canonical 的收件人：``→ @Elaina`` / ``→ [归属不明]``；记录里没写目标的
    只有 ``谁:`` 一段，抽取规则要求写「某人」，不许从内容猜。
    """
    from core.context.attribution import (
        attribution_enabled,
        attribution_target,
        default_context,
    )

    ctx = default_context(
        identities=identities or {},
        display_names=display_names or {},
        is_group=True,
    ) if attribution_enabled() else None
    lines: list[str] = []
    ids: set[str] = set()
    for row in events:
        event_id = str(row.get("event_id") or "")
        ids.add(event_id)
        if ctx is None:
            # 开关关掉：退回旧形制（只有"谁: 内容"，不写对谁）。
            try:
                meta = json.loads(row.get("metadata_json") or "{}")
            except Exception:  # noqa: BLE001
                warn("core/quality/memory_proposals.py:278 Exception", err=repr(__import__("sys").exc_info()[1]))
                meta = {}
            actor = str(row.get("actor_person_id") or "")
            who = "鲸鲸" if (
                row.get("event_type") in ("reply", "thought") or meta.get("is_self") == 1
            ) else (meta.get("display_name") or names.get(actor) or actor[:8] or "?")
            lines.append(f"[{event_id}] {who}: {_flat(row.get('content'), 120)}")
            continue
        attribution = ctx.resolve(row)
        if attribution is None:
            continue
        who = "鲸鲸" if attribution.speaker.is_self else (
            attribution.speaker.display_name or names.get(
                str(row.get("actor_person_id") or ""), ""
            ) or "?"
        )
        target = attribution_target(attribution)
        arrow = f" → {target}" if target else ""
        lines.append(f"[{event_id}] {who}{arrow}: {_flat(row.get('content'), 120)}")
    return "\n".join(lines), ids


async def propose_from_chat(
    chat_id: str,
    *,
    hours: int = 24,
    limit: int = 60,
    llm: Any = None,
) -> dict:
    """Extract candidates for one chat and stage them (status=pending)."""
    from core.terminal.store_port import get_meta_repo # local: keeps module import cheap

    stats = {"events": 0, "raw": 0, "staged": 0, "skipped": 0}
    try:
        engine = get_meta_repo().engine
        with engine.connect() as conn:
            events = _load_events(conn, chat_id, hours, limit)
            names = _speakers(conn)
            identities, display_names = _identity_index(conn)
        stats["events"] = len(events)
        if len(events) < 5 or llm is None:
            return stats
        listing, valid_ids = _render_events(
            events, names, identities=identities, display_names=display_names
        )
        response = await llm.chat(
            system=_system_prompt(),
            user=f"【记录·chat={chat_id}】\n{listing}",
            agent_name="memory_proposal",
            temperature=0.1,
            max_tokens=800,
        )
        parsed = llm.parse_json(response, {"proposals": []})
        raw = parsed.get("proposals") if isinstance(parsed, dict) else None
        if not isinstance(raw, list):
            return stats
        stats["raw"] = len(raw)
        stats["staged"] = _stage(chat_id, raw, valid_ids, names)
        stats["skipped"] = len(raw) - stats["staged"]
    except Exception as exc:  # noqa: BLE001
        log.warning("memory_proposal_failed", chat_id=chat_id, error=str(exc)[:160])
    return stats


def _validate_item(item: Any, valid_ids: set[str]) -> dict | None:
    """纯校验：槽位/内容/证据/理由。不过关返回 None（宁缺毋滥）。

    批 3c：v2 开关打开时**只接受** ``SLOTS_V2``（open_loop / habit），
    身份/偏好/约定类一律丢弃；并要求带 ``evidence_text``（原话片段）。
    """
    if not isinstance(item, dict):
        return None
    v2 = proposals_v2_enabled()
    allowed = SLOTS_V2 if v2 else SLOTS
    slot = str(item.get("slot") or "").strip()
    content = _flat(item.get("content"))
    if slot not in allowed or len(content) < 4:
        return None
    evidence = [str(e) for e in (item.get("evidence") or []) if str(e) in valid_ids][:6]
    if not evidence:
        return None
    reason = str(item.get("reason") or "llm").strip()
    if reason not in REASONS:
        reason = "llm"
    subject = str(item.get("subject") or "").strip()[:20]
    # 习惯槽不许出现"要求/命令"式措辞（玩笑里的规则一旦沉淀，她会拿来拒绝配合）
    if slot == "habit" and any(marker in content for marker in _INSTRUCTION_MARKERS):
        return None
    # 说她自己的"习惯"是回声，不是对人的认识
    if slot == "habit" and subject in _BOT_SUBJECTS:
        return None
    result = {
        "slot": slot,
        "content": content,
        "evidence": evidence,
        "subject": subject,
        "reason": reason,
    }
    if v2:
        # 「重复模式」要有统计支撑：证据条数即出现次数
        if slot == "habit" and len(evidence) < REPEAT_MIN_OCCURRENCES:
            return None
        text = _flat(item.get("evidence_text"), 40)
        if not text:
            return None  # 没带原话证据 = 不可审，丢弃
        result["evidence_text"] = text
    return result


def _stage(chat_id: str, raw: list, valid_ids: set[str], names: dict[str, str]) -> int:
    """Validate + dedupe + insert.  Returns how many were staged."""
    from core.terminal.store_port import get_meta_repo
    from sqlalchemy import text as _sql

    existing = _existing_contents(chat_id)
    pending = _pending_counts()
    staged = 0
    with get_meta_repo().engine.begin() as conn:
        for item in raw:
            checked = _validate_item(item, valid_ids)
            if checked is None:
                continue
            slot = checked["slot"]
            content = checked["content"]
            evidence = checked["evidence"]
            subject = checked["subject"]
            key = _resolve_key(subject, names)
            if _duplicate(content, existing):
                continue
            if pending.get((slot, subject), 0) >= MAX_PER_SUBJECT:
                continue
            conn.execute(
                _sql(
                    "insert into memory_proposals "
                    "(slot, subject_name, subject_key, chat_id, platform, content, reason, "
                    " evidence_json, status, created_at) "
                    "values (:slot, :subject_name, :subject_key, :chat_id, '', :content, :reason, "
                    " :evidence, 'pending', :now)"
                ),
                {
                    "slot": slot,
                    "subject_name": subject,
                    "subject_key": key,
                    "chat_id": str(chat_id),
                    "content": content,
                    "reason": checked["reason"],
                    # 批 3c：证据除事件 id 外附原话片段，审阅不必回库查
                    "evidence": json.dumps(
                        {
                            "ids": evidence,
                            "text": checked.get("evidence_text", ""),
                        },
                        ensure_ascii=False,
                    ),
                    "now": _now(),
                },
            )
            pending[(slot, subject)] = pending.get((slot, subject), 0) + 1
            existing.add(content)
            staged += 1
    return staged


def _resolve_key(subject: str, names: dict[str, str]) -> str:
    if not subject:
        return ""
    for wxid, name in names.items():
        if str(name) == subject:
            return wxid
    return ""


def _existing_contents(chat_id: str) -> set[str]:
    from core.terminal.store_port import get_meta_repo
    from sqlalchemy import text as _sql

    out: set[str] = set()
    with get_meta_repo().engine.connect() as conn:
        for row in conn.execute(
            _sql(
                "select content from person_notes where active = 1 "
                "and (chat_id = :chat or chat_id = '')"
            ),
            {"chat": str(chat_id)},
        ).mappings():
            out.add(_flat(row["content"]))
        for row in conn.execute(
            _sql("select content from memory_proposals where status = 'pending'")
        ).mappings():
            out.add(_flat(row["content"]))
    return out


def _pending_counts() -> dict[tuple[str, str], int]:
    from core.terminal.store_port import get_meta_repo
    from sqlalchemy import text as _sql

    out: dict[tuple[str, str], int] = {}
    with get_meta_repo().engine.connect() as conn:
        for row in conn.execute(
            _sql(
                "select slot, subject_name, count(*) n from memory_proposals "
                "where status = 'pending' group by slot, subject_name"
            )
        ).mappings():
            out[(str(row["slot"]), str(row["subject_name"]))] = int(row["n"])
    return out


def _duplicate(content: str, existing: set[str]) -> bool:
    flat = _flat(content)
    if not flat:
        return True
    if flat in existing:
        return True
    for other in existing:
        if len(other) >= 6 and (other in flat or flat in other):
            return True
    return False


# ------------------------------------------------------------------ review
def list_proposals(status: str = "pending", chat_id: str = "") -> list[dict]:
    from core.terminal.store_port import get_meta_repo
    from sqlalchemy import text as _sql

    sql = (
        "select id, slot, subject_name, subject_key, chat_id, content, reason, "
        "evidence_json, status, created_at, decided_at from memory_proposals"
    )
    clauses, params = [], {}
    if status and status != "all":
        clauses.append("status = :status")
        params["status"] = str(status)
    if chat_id:
        clauses.append("chat_id = :chat")
        params["chat"] = str(chat_id)
    if clauses:
        sql += " where " + " and ".join(clauses)
    sql += " order by id"
    with get_meta_repo().engine.connect() as conn:
        return [dict(r) for r in conn.execute(_sql(sql), params).mappings()]


_LEXICON_TERM_RE = re.compile(r"「([^」]{1,20})」")


def _lexicon_line(proposal_content: str, final: str) -> str:
    """术语提案 → 群级备注里的一行：`词=释义`。

    释义缺失写明「待补释义」——自动化系统没资格替词条下定义。
    """
    match = _LEXICON_TERM_RE.search(str(proposal_content or ""))
    term = match.group(1).strip() if match else ""
    text = _flat(final, 60)
    if not term:
        return text
    return f"{term}={text}" if text else f"{term}=待补释义"


def _merge_lexicon(existing: str, proposal_content: str, final: str) -> str:
    """群级备注里的词条行（`词=释义`），`|` 分隔、便于逐行渲染。

    同名多义（硬条件 A）：同一词条的不同释义**并存**，只去重完全相同的行；
    纠正旧释义由主人编辑/删除该行（纠错契约），不再"新释义覆盖旧释义"。
    """
    line = _lexicon_line(proposal_content, final)
    if not line:
        return str(existing or "")
    parts = [p.strip() for p in str(existing or "").replace("\n", "|").split("|") if p.strip()]
    if line in parts:
        return " | ".join(parts)
    parts.append(line)
    return " | ".join(parts)


def _merge_note(existing: str, new: str) -> str:
    """批准写入时不覆盖手写备注：包含则不变，否则追加。"""
    old = _flat(existing, 200)
    fresh = _flat(new)
    if not old:
        return fresh
    if not fresh or fresh in old:
        return old
    if old in fresh:
        return fresh
    return f"{old.rstrip('。；;')}；{fresh}"


def approve(proposal_id: int, *, content: str = "", decided_by: str = "owner") -> bool:
    """批准 → 写入 person_notes（下一轮就能看到她眼前）。已有备注则合并。"""
    from core.terminal.store_port import get_meta_repo
    from sqlalchemy import text as _sql

    from core.context.person_notes import upsert_note

    with get_meta_repo().engine.connect() as conn:
        row = conn.execute(
            _sql(
                "select id, slot, subject_name, subject_key, chat_id, content, "
                "evidence_json, status from memory_proposals where id = :id"
            ),
            {"id": int(proposal_id)},
        ).mappings().first()
        if row is None or str(row["status"]) != "pending":
            return False
        current = conn.execute(
            _sql(
                "select content from person_notes where person_key = :key "
                "and chat_id = :chat and active = 1"
            ),
            {"key": str(row["subject_key"] or ""), "chat": str(row["chat_id"] or "")},
        ).mappings().first()
    slot = str(row["slot"])
    final = _flat(content) or _flat(row["content"])
    existing_note = str(current["content"]) if current is not None else ""
    if slot == "lexicon":
        # 术语写群级备注的独立一行（`|` 分隔），不挤进成员清单同一行
        note = _merge_lexicon(existing_note, str(row["content"]), final)
    else:
        note = _merge_note(
            existing_note,
            f"{SLOTS.get(slot, slot)}：{final}",
        )
    upsert_note(
        person_key=str(row["subject_key"] or ""),
        content=note,
        chat_id=str(row["chat_id"] or ""),
        source="auto",
        source_proposal_id=int(row["id"]),
        evidence_json=str(row["evidence_json"] or ""),
    )
    with get_meta_repo().engine.begin() as conn:
        conn.execute(
            _sql(
                "update memory_proposals set status = 'approved', content = :content, "
                "decided_at = :now, decided_by = :by where id = :id"
            ),
            {"content": final, "now": _now(), "by": str(decided_by), "id": int(proposal_id)},
        )
    return True


def reject(proposal_id: int, *, decided_by: str = "owner") -> bool:
    from core.terminal.store_port import get_meta_repo
    from sqlalchemy import text as _sql

    with get_meta_repo().engine.begin() as conn:
        result = conn.execute(
            _sql(
                "update memory_proposals set status = 'rejected', decided_at = :now, "
                "decided_by = :by where id = :id and status = 'pending'"
            ),
            {"now": _now(), "by": str(decided_by), "id": int(proposal_id)},
        )
    return bool(result.rowcount)


def revoke(proposal_id: int, *, decided_by: str = "owner") -> bool:
    """撤回已经批准的提案：把写进备注的那一段删掉，提案置 revoked。

    只删本提案追加的那段（``槽位：内容``），不动手写的原文。
    """
    from core.terminal.store_port import get_meta_repo
    from sqlalchemy import text as _sql

    from core.context.person_notes import upsert_note

    with get_meta_repo().engine.connect() as conn:
        row = conn.execute(
            _sql(
                "select id, slot, subject_key, chat_id, content, status "
                "from memory_proposals where id = :id"
            ),
            {"id": int(proposal_id)},
        ).mappings().first()
        if row is None or str(row["status"]) != "approved":
            return False
        current = conn.execute(
            _sql(
                "select content from person_notes where person_key = :key "
                "and chat_id = :chat"
            ),
            {"key": str(row["subject_key"] or ""), "chat": str(row["chat_id"] or "")},
        ).mappings().first()
    note = str(current["content"]) if current is not None else ""
    if str(row["slot"]) == "lexicon":
        parts = [p.strip() for p in note.replace("\n", "|").split("|") if p.strip()]
        term = _lexicon_line(str(row["content"]), "").split("=", 1)[0]
        kept = [p for p in parts if p.split("=", 1)[0] != term]
        if len(kept) != len(parts):
            note = " | ".join(kept)
            if current is not None:
                upsert_note(
                    person_key=str(row["subject_key"] or ""),
                    content=note,
                    chat_id=str(row["chat_id"] or ""),
                    source="owner",
                    clear_provenance=True,
                )
    else:
        addition = f"{SLOTS.get(str(row['slot']), str(row['slot']))}：{_flat(row['content'])}"
        if addition in note:
            note = note.replace("；" + addition, "").replace(addition, "").strip("；; ")
            if current is not None:
                upsert_note(
                    person_key=str(row["subject_key"] or ""),
                    content=note,
                    chat_id=str(row["chat_id"] or ""),
                    source="owner",
                    clear_provenance=True,
                )
    with get_meta_repo().engine.begin() as conn:
        conn.execute(
            _sql(
                "update memory_proposals set status = 'revoked', decided_at = :now, "
                "decided_by = :by where id = :id"
            ),
            {"now": _now(), "by": str(decided_by), "id": int(proposal_id)},
        )
    return True


__all__ = [
    "SLOTS",
    "approve",
    "list_proposals",
    "propose_from_chat",
    "reject",
]
