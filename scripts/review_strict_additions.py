"""Generate a human-review sheet for strict canary additions and misses.

Reads ``logs/decision_trace.jsonl`` (or the path given as argv[1]) and writes
``output/strict_additions_review_YYYYMMDD.md``.  The sheet is the input for the
manual review gate (灰度手册已归档 `backups/docs_pruned_*`) §7.4: every strict addition must
be judged as "reasonable" or "interjection" before widening the canary.

The trace's ``event`` dict is canonical (not legacy), so it is converted back to
``MessageEvent`` locally.  Only the signal fields needed for reviewing the ack
fix are re-extracted; window state is deliberately not replayed.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.contracts import ChatType, MessageEvent, MessageKind
from core.conversation.policy import strict_reply_allowed
from core.conversation.signals import extract_signals

TRACE = "logs/decision_trace.jsonl"
OUT_DIR = Path("output")


def event_from_trace(data: dict) -> MessageEvent:
    conversation = data.get("conversation") or {}
    chat_type = {
        "group": ChatType.GROUP,
        "private": ChatType.PRIVATE,
    }.get(str(conversation.get("chat_type") or "group").lower(), ChatType.UNKNOWN)
    try:
        kind = MessageKind(data.get("kind") or "text")
    except ValueError:
        kind = MessageKind.UNKNOWN
    created_at = None
    if data.get("created_at"):
        try:
            created_at = dt.datetime.fromisoformat(data["created_at"])
        except ValueError:
            created_at = None
    return MessageEvent.new(
        platform=data.get("platform") or "wechat",
        account_id=data.get("account_id") or "",
        chat_id=conversation.get("chat_id") or "",
        chat_type=chat_type,
        message_id=data.get("message_id") or "",
        sender_id=data.get("sender_id") or "",
        sender_name=data.get("sender_name") or "",
        content=data.get("content") or "",
        kind=kind,
        reply_to_id=data.get("reply_to_id") or "",
        created_at=created_at,
    )


def _legacy_reply(row: dict) -> bool:
    return bool((row.get("legacy") or {}).get("should_reply"))


def _shadow(row: dict) -> dict:
    return row.get("shadow") or {}


# Known bad-pattern tags from 灰度执行手册 §21（手册已归档 backups/docs_pruned_*）.  These are review aids,
# not verdicts: the human still fills the last column.
_AUTO_FLAGS: tuple[tuple[str, str], ...] = (
    ("噢", "ack 变体：本版已修"),
    ("我也不知道", "P1 连续反问循环：未知答案需 TurnIntent 承接"),
    ("不知道", "P1 未知/无信息答案"),
    ("看见没", "P2 疑似 @ 错人"),
    ("好相处吗", "P2 疑似评价群成员"),
    ("主人", "内容风险：关系越界"),
)


def _auto_tag(content: str) -> str:
    tags = [tag for marker, tag in _AUTO_FLAGS if marker in content]
    return "；".join(tags)


_GROUP_DIRECTED_MARKERS = (
    "怎么没人来说话", "你们这么安静", "你开点话题", "群里太安静",
)


def _suggest_verdict(
    content: str,
    signals,
    *,
    dialogue_act: str,
    score: float,
    ack_fixed: bool,
) -> str:
    """Best-effort review suggestion; the human column remains authoritative."""
    if ack_fixed:
        return "误插（ack 修复后消失）"
    if getattr(signals, "unknown_answer", False):
        return "合理回复一次，但禁止再追问（TurnIntent 已加约束）"
    if "看见没" in content:
        return "误插：@错人检查"
    if "好相处吗" in content or ("这个人" in content and "吗" in content):
        return "高风险：第三人评价 / 隐私，建议 ReplyCritic warn-only"
    if "主人" in content:
        return "高风险：关系越界，建议 ReplyCritic warn-only"
    if any(marker in content for marker in _GROUP_DIRECTED_MARKERS):
        return "疑似误插：更像对群说，不是回答 Bot"
    if dialogue_act == "answer_to_bot" and score >= 0.6:
        return "合理：对 Bot 问题的回答"
    if dialogue_act == "question_to_bot":
        return "待人工：对 Bot 提问，需看上下文"
    return "待人工：软继续"


def _event_time(row: dict):
    raw = (row.get("event") or {}).get("created_at")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def _window_from_trace(row: dict):
    """Rebuild the window snapshot stored in a trace row for signal replay."""
    from core.conversation.domain import ConversationWindow, WindowState

    data = row.get("window") or {}
    event = row.get("event") or {}
    conversation = event.get("conversation") or {}
    try:
        state = WindowState(str(data.get("state") or "idle"))
    except ValueError:
        state = WindowState.IDLE
    if state in (WindowState.IDLE, WindowState.CLOSED):
        return None
    created_at = None
    if event.get("created_at"):
        try:
            created_at = dt.datetime.fromisoformat(event["created_at"])
        except ValueError:
            created_at = None
    window = ConversationWindow(
        platform=event.get("platform") or "wechat",
        account_id=event.get("account_id") or "",
        chat_id=conversation.get("chat_id") or "",
        person_id=str(data.get("person_id") or event.get("sender_id") or ""),
        state=state,
        last_bot_message_id=str(data.get("last_bot_message_id") or ""),
        last_bot_asked_question=bool(data.get("last_bot_asked_question")),
        turn_count=int(data.get("turn_count") or 0),
        opened_at=created_at,
        last_activity_at=created_at,
    )
    return window


def load_rows(trace: str) -> list[dict]:
    rows: list[dict] = []
    path = Path(trace)
    if not path.exists():
        raise SystemExit(f"trace not found: {trace}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("kind") == "addressee_shadow":
            rows.append(row)
    return rows


def main() -> int:
    trace = sys.argv[1] if len(sys.argv) > 1 else TRACE
    rows = load_rows(trace)

    legacy_reply = sum(_legacy_reply(row) for row in rows)
    shadow_reply = sum(_shadow(row).get("action") == "reply" for row in rows)
    strict = [
        row for row in rows
        if not _legacy_reply(row)
        and _shadow(row).get("action") == "reply"
        and strict_reply_allowed(
            _shadow(row).get("signals") or {},
            score=float(_shadow(row).get("score") or 0),
        )
    ]
    misses = [
        row for row in rows
        if _legacy_reply(row) and _shadow(row).get("action") != "reply"
    ]

    strict_signals = [
        (
            row,
            extract_signals(
                event_from_trace(row.get("event") or {}),
                window=_window_from_trace(row),
                now=_event_time(row),
            ),
        )
        for row in strict
    ]
    ack_fixed = [row for row, signals in strict_signals if signals.ack]
    verdicts: dict[int, str] = {}
    for index, (row, signals) in enumerate(strict_signals):
        event = row.get("event") or {}
        shadow = _shadow(row)
        content = (event.get("content") or "").replace("|", "/").replace("\n", " ")[:60]
        verdicts[index] = _suggest_verdict(
            content, signals,
            dialogue_act=str(shadow.get("dialogue_act") or ""),
            score=float(shadow.get("score") or 0),
            ack_fixed=bool(signals.ack),
        )
    verdict_counts: dict[str, int] = {}
    for verdict in verdicts.values():
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

    now = dt.datetime.now()
    lines = [
        "# 鲸鲸 strict additions 人工审核清单",
        "",
        f"- 生成时间：{now:%Y-%m-%d %H:%M:%S}",
        f"- 数据源：`{trace}`（{len(rows)} 行）",
        f"- legacy reply {legacy_reply} / shadow reply {shadow_reply}",
        f"- strict additions {len(strict)} / miss {len(misses)}",
        f"- 本次 ack 修复可直接消掉：{len(ack_fixed)} 条",
        "",
        "> 审核目标：逐条判断是“合适新增”还是“误插”。已知坏例分类见《灰度执行手册》§21（手册已归档 backups/docs_pruned_*）。",
        "> “建议结论（AI）”是规则判断，只用于加速人工审核；最终以最后一列的人工结论为准。",
        "",
        "### 建议结论分布",
        "",
    ]
    for verdict, count in sorted(verdict_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {count} 条：{verdict}")
    lines += [
        "",
        "## 一、strict additions（legacy 沉默、strict 会新增回复）",
        "",
        "| # | 时间 | 会话 | 发送者 | 内容 | reason | score | act | ack修复 | 自动标记 | 建议结论（AI） | 人工结论 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for index, (row, signals) in enumerate(strict_signals, 1):
        event = row.get("event") or {}
        conversation = event.get("conversation") or {}
        shadow = _shadow(row)
        content = (event.get("content") or "").replace("|", "/").replace("\n", " ")[:60]
        lines.append(
            f"| {index} | {event.get('created_at', '')} "
            f"| {conversation.get('chat_id', '')} "
            f"| {event.get('sender_name', '')} "
            f"| {content} "
            f"| {shadow.get('reason', '')} "
            f"| {shadow.get('score', '')} "
            f"| {shadow.get('dialogue_act', '')} "
            f"| {'是' if signals.ack else ''} "
            f"| {_auto_tag(content)} "
            f"| {verdicts[index - 1]} |  |"
        )

    lines += [
        "",
        "## 二、miss（legacy 会回、shadow/strict 不回）",
        "",
        "> strict 只作用于群；私聊 / 拍一拍由 legacy 照常回复，不计入 strict 放行判断。",
        "",
        "| # | 时间 | 会话 | 内容 | legacy reason | shadow reason | score | 人工结论 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for index, row in enumerate(misses, 1):
        event = row.get("event") or {}
        conversation = event.get("conversation") or {}
        shadow = _shadow(row)
        legacy = row.get("legacy") or {}
        content = (event.get("content") or "").replace("|", "/").replace("\n", " ")[:60]
        lines.append(
            f"| {index} | {event.get('created_at', '')} "
            f"| {conversation.get('chat_id', '')} "
            f"| {content} "
            f"| {legacy.get('reason', '')} "
            f"| {shadow.get('reason', '')} "
            f"| {shadow.get('score', '')} |  |"
        )

    lines += [
        "",
        "## 三、备注",
        "",
        "- `ack修复=是` 表示本次 `_ACK_PHRASES` 扩充后会从 strict additions 中消失。",
        "- 其余条目需人工判断，尤其 continuation 里的“软继续”。",
        f"- 更新后的 strict additions 预估：{len(strict) - len(ack_fixed)} 条。",
        "- 把人工结论填进最后一列，作为 Step 3 放行判断的输入。",
        "",
    ]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"strict_additions_review_{now:%Y%m%d}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {out_path}")
    print(
        f"total={len(rows)} legacy_reply={legacy_reply} "
        f"shadow_reply={shadow_reply} strict={len(strict)} miss={len(misses)} "
        f"ack_fixed={len(ack_fixed)} strict_after={len(strict) - len(ack_fixed)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
