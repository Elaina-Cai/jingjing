"""Chat window builder: attribution rules, graded degradation and invariants.

See docs/记忆的定义.md §1.  All assertions here are the ones promised to
run_all_tests (条数守恒 / 归属守恒 / 顺序守恒 / 片段下限 / 时间桶连续).

归属：窗口构造与断言（归因/条数守恒/顺序/片段下限/预算/旗标）。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context.chat_window import (  # noqa: E402
    WindowMessage,
    _buckets,
    _dedupe_self_echoes,
    _fmt_full,
    _fmt_skeleton,
    _is_pat_content,
    _is_revoke_content,
    _pat_text,
    _revoke_text,
    _taper,
    build_chat_window,
    messages_from_rows,
    window_enabled_for,
)


def _msgs(count: int, start: datetime | None = None) -> list[WindowMessage]:
    start = start or datetime.now().replace(hour=6, minute=0, second=0, microsecond=0)
    people = ["Y-K", "P8", "鲸鲸", "幼儿园第一老太", "Elaina"]
    out: list[WindowMessage] = []
    for i in range(count):
        out.append(
            WindowMessage(
                occurred_at=start + timedelta(minutes=3 * i),
                speaker=people[i % len(people)],
                content=f"第{i}条消息内容，测试用",
            )
        )
    return out


# --------------------------------------------------------------- attribution
def test_attribution_reply_is_the_bot() -> None:
    """reply/thought 的 actor 存的是收件人 —— 说话人必须是鲸鲸。"""
    rows = [
        {
            "occurred_at": "2026-09-22 14:19:00",
            "event_type": "reply",
            "actor_person_id": "wxid_seed15",
            "content": "Y-K 这「叫爸爸」的，我可不上钩。",
            "metadata_json": "{}",
        },
        {
            "occurred_at": "2026-09-22 14:18:00",
            "event_type": "message",
            "actor_person_id": "wxid_seed15",
            "content": "叫爸爸",
            "metadata_json": "{}",
        },
        {
            "occurred_at": "2026-09-22 14:20:00",
            "event_type": "message",
            "actor_person_id": "wxid_seed10",
            "content": "开智了",
            "metadata_json": '{"is_self": 0}',
        },
    ]
    names = {"wxid_seed15": "Y-K", "wxid_seed10": "P8"}
    msgs = messages_from_rows(rows, names)
    assert msgs[0].speaker == "鲸鲸", msgs[0]
    assert msgs[1].speaker == "Y-K", msgs[1]
    assert msgs[2].speaker == "P8", msgs[2]


def test_attribution_is_self_flag_wins() -> None:
    rows = [
        {
            "occurred_at": "2026-09-22 17:50:00",
            "event_type": "question",
            "actor_person_id": "wxid_seed02",
            "content": "修好了吗手机",
            "metadata_json": '{"is_self": 1}',
        }
    ]
    msgs = messages_from_rows(rows, {"wxid_seed02": "鲸鲸账号"})
    assert msgs[0].speaker == "鲸鲸", msgs[0]


# ------------------------------------------------------------------ window
def test_recent_kept_verbatim_and_older_compressed() -> None:
    msgs = _msgs(120)
    text = build_chat_window("test@chatroom", messages=msgs, full=40, budget_chars=600)
    assert "【最近对话·原文】" in text
    # 最近 40 条必须原样出现（含完整内容）
    assert "第119条消息内容，测试用" in text
    for msg in msgs[-40:]:
        assert msg.content in text
    # 预算紧张时更早的部分必须被压缩（不能原文照搬）
    assert "第0条消息内容，测试用" not in text
    assert "【今天更早·压缩】" in text


def test_every_message_is_reachable_in_compressed_part() -> None:
    """条数守恒：压缩段覆盖更早的全部条数，说话人也都在。"""
    msgs = _msgs(120)
    text = build_chat_window("test@chatroom", messages=msgs, full=40, budget_chars=20000)
    older = msgs[:-40]
    speakers = {m.speaker for m in older}
    for name in speakers:
        assert name in text, name
    assert "第0条消息内容" in text  # 预算足够时逐条骨架保留最早一条


def test_budget_never_drops_before_hour_digest() -> None:
    """预算极小 → 退到小时摘要，仍要保留条数与说话人。"""
    msgs = _msgs(200)
    text = build_chat_window("test@chatroom", messages=msgs, full=40, budget_chars=200)
    assert text.count("共 ") >= 1
    assert "【最近对话·原文】" in text


def test_snippet_floor() -> None:
    """片段下限：任何片段都要么 ≥8 字，要么是「（…共 N 条）」形式的标注。"""
    msgs = _msgs(160)
    text = build_chat_window("test@chatroom", messages=msgs, full=40, budget_chars=900)
    for line in text.splitlines():
        if not line.startswith("[") or ": " not in line:
            continue
        body = line.split("] ", 1)[-1]
        for part in body.split(" ／ "):
            fragment = part.split(": ", 1)[-1].rstrip("…").strip()
            if fragment.startswith("（") or "共 " in line:
                continue
            assert len(fragment) >= 8, f"片段过短: {part!r}"


def test_current_message_appended_once() -> None:
    msgs = _msgs(5)
    current = WindowMessage(
        occurred_at=msgs[-1].occurred_at + timedelta(seconds=20),
        speaker="Y-K",
        content="最后一条新消息",
    )
    text = build_chat_window("test@chatroom", messages=msgs, current=current, full=40)
    assert text.count("最后一条新消息") == 1
    # 已在库里的消息不会再被追加一次
    dup = build_chat_window("test@chatroom", messages=msgs, current=msgs[-1], full=40)
    assert dup.count(msgs[-1].content) == 1


def test_recent_part_has_char_cap() -> None:
    """长消息（她的长回复）不能把"最近原文"撑爆：条数+字数双上限。"""
    start = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    msgs = [
        WindowMessage(start + timedelta(minutes=i), "鲸鲸", "长" * 400) for i in range(60)
    ]
    text = build_chat_window("t@chatroom", messages=msgs, full=40, budget_chars=3600)
    section = text.split("【最近对话·原文】")[1].split("【")[0]
    assert section.count("\n") + 1 <= 10, section.count("\n")
    assert len(text) < 3600 + 500, len(text)


def test_self_echo_deduped() -> None:
    """她的同一句话（消息同步 is_self + 自己写的 reply）只留一条。"""
    base = datetime.now().replace(hour=14, minute=0, second=0, microsecond=0)
    rows = [
        WindowMessage(base, "鲸鲸", "这句话挺长的，会被存两遍", is_self=True),
        WindowMessage(base + timedelta(seconds=4), "鲸鲸", "这句话挺长的，会被存两遍", is_self=True),
        WindowMessage(base + timedelta(seconds=5), "Y-K", "我靠", is_self=False),
        WindowMessage(base + timedelta(seconds=6), "Y-K", "我靠", is_self=False),
    ]
    kept = _dedupe_self_echoes(rows)
    assert len(kept) == 3, kept  # 她的话留 1 条；别人的短重复不误伤
    assert [m.speaker for m in kept].count("鲸鲸") == 1


def test_self_echo_kept_when_far_apart() -> None:
    base = datetime.now().replace(hour=14, minute=0, second=0, microsecond=0)
    rows = [
        WindowMessage(base, "鲸鲸", "同一句很长的话重复说", is_self=True),
        WindowMessage(base + timedelta(minutes=30), "鲸鲸", "同一句很长的话重复说", is_self=True),
    ]
    assert len(_dedupe_self_echoes(rows)) == 2


def test_order_is_preserved() -> None:
    msgs = _msgs(90)
    text = build_chat_window("test@chatroom", messages=msgs, full=40, budget_chars=5000)
    positions = [text.index(m.content) for m in msgs[-40:]]
    assert positions == sorted(positions)


# -------------------------------------------------------------------- flags
def test_unattributed_quote_is_marked() -> None:
    """批2b 收口：认不出作者的引用行，渲染时显式标出（不装懂）。"""
    start = datetime(2026, 9, 23, 13, 0, 0)
    rows = [
        WindowMessage(occurred_at=start, speaker="P5", content="跑死了"),
        WindowMessage(
            occurred_at=start + timedelta(seconds=30),
            speaker="P4",
            content="[引用 P5: 跑死了] 潘神每天起床",
        ),
        WindowMessage(
            occurred_at=start + timedelta(seconds=60),
            speaker="P8",
            content="[引用: 跑死了] 什么跟什么",
        ),
    ]
    # 钉死 now：窗口只把「今天」的行放进原文段，用真实时钟会让这个固定日期
    # 的用例在跨天后静默失效（09-23 写入、09-24 起全被压缩进摘要）。
    text = build_chat_window(
        "t@chatroom", messages=rows, full=40,
        now=start + timedelta(minutes=2),
    )
    assert "[引用 P5: 跑死了] 潘神每天起床" in text, text
    assert "[引用〔作者不明〕: 跑死了] 什么跟什么" in text, text


def test_flag_scoping() -> None:
    os.environ.pop("YINYUE_FEATURE_CHAT_WINDOW", None)
    os.environ.pop("YINYUE_CHAT_WINDOW_CHATS", None)
    assert window_enabled_for("a@chatroom") is False
    os.environ["YINYUE_FEATURE_CHAT_WINDOW"] = "1"
    assert window_enabled_for("a@chatroom") is True  # 空名单 = 全部
    os.environ["YINYUE_CHAT_WINDOW_CHATS"] = "a@chatroom"
    assert window_enabled_for("a@chatroom") is True
    assert window_enabled_for("b@chatroom") is False
    os.environ.pop("YINYUE_FEATURE_CHAT_WINDOW", None)
    os.environ.pop("YINYUE_CHAT_WINDOW_CHATS", None)


# ---------------------------------------------------------------------- pats
# Q1（2026-09-24 群事故）：拍一拍曾被渲染成普通聊天行
# `[18:51] P6: "P6" 拍了拍 "鲸鲸"`，模型把不同人的拍都算成"拍我"、还数错。
# 现在拍一拍=系统通知：打 ``（系统）`` 标记、去掉系统引号、不带 `X: ` 说话人前缀。
def test_pat_content_detection() -> None:
    assert _is_pat_content('"P6" 拍了拍 "鲸鲸"') is True
    assert _is_pat_content('"Elaina" 拍了拍我') is True
    assert _is_pat_content('我拍了拍 "P6"') is True
    assert _is_pat_content('"P6" 拍了拍 "鲸鲸" 说“明天见”') is True
    # 聊天正文里的"拍了拍"不是系统事件
    assert _is_pat_content("他拍了拍桌子就走了") is False
    assert _is_pat_content("拍了拍") is False
    assert _is_pat_content("") is False


def test_pat_rows_are_flagged() -> None:
    start = datetime(2026, 9, 24, 18, 51, 0)
    rows = [
        {
            "occurred_at": start,
            "event_type": "message",
            "actor_person_id": "wxid_seed06",
            "content": '"P6" 拍了拍 "鲸鲸"',
            "metadata_json": "{}",
        },
        {
            "occurred_at": start + timedelta(minutes=1),
            "event_type": "message",
            "actor_person_id": "wxid_seed06",
            "content": "晚上不想上课怎么办",
            "metadata_json": "{}",
        },
        {
            "occurred_at": start + timedelta(minutes=2),
            "event_type": "reply",
            "actor_person_id": "wxid_bot",
            "content": '我拍了拍 "P6"',
            "metadata_json": "{}",
        },
    ]
    msgs = messages_from_rows(rows, {"wxid_seed06": "P6"})
    assert [m.is_pat for m in msgs] == [True, False, True], [m.is_pat for m in msgs]


def test_pat_rendered_as_system_notice() -> None:
    start = datetime(2026, 9, 24, 18, 51, 0)
    pat = WindowMessage(start, "P6", '"P6" 拍了拍 "鲸鲸"', is_pat=True)
    assert _pat_text(pat) == "（系统）P6 拍了拍 鲸鲸"
    assert _fmt_full(pat) == "[18:51] （系统）P6 拍了拍 鲸鲸"
    assert _fmt_skeleton(pat, 20) == "[18:51] （系统）P6 拍了拍 鲸鲸"
    assert _taper([pat], (20, 12, 8)) == ["[18:51] （系统）P6 拍了拍 鲸鲸"]
    # 时间桶按桶首取整（10 分钟桶 → 18:50）
    assert _buckets([pat], minutes=10) == ["[18:50] （系统）P6 拍了拍 鲸鲸"]
    # 普通消息保持"谁说的、说了什么"
    normal = WindowMessage(start, "P6", "晚上不想上课怎么办")
    assert _fmt_full(normal) == "[18:51] P6: 晚上不想上课怎么办"
    assert _fmt_skeleton(normal, 20) == "[18:51] P6: 晚上不想上课怎么办"


def test_revoke_rendered_as_system_notice() -> None:
    """批B：撤回消息（revokemsg XML）按系统样式渲染，不冒充"某人说的话"。"""
    raw = (
        '<?xml version="1.0"?><sysmsg type="revokemsg"><revokemsg>'
        '<content>"P6" 撤回了一条消息</content><revoketime>0</revoketime>'
        "</revokemsg></sysmsg>"
    )
    assert _is_revoke_content(raw) is True
    assert _is_revoke_content("他拍了拍桌子就走了") is False
    msg = WindowMessage(datetime(2026, 9, 24, 18, 51), "P6", raw, is_revoke=True)
    assert _revoke_text(msg) == "（系统）P6 撤回了一条消息"
    assert _fmt_full(msg) == "[18:51] （系统）P6 撤回了一条消息"
    assert _fmt_skeleton(msg, 20) == "[18:51] （系统）P6 撤回了一条消息"
    assert _taper([msg], (20, 12, 8)) == ["[18:51] （系统）P6 撤回了一条消息"]
    rows = [
        {
            "occurred_at": "2026-09-24 18:51:00",
            "event_type": "question",
            "actor_person_id": "wxid_seed06",
            "content": raw,
            "metadata_json": "{}",
        },
        {
            "occurred_at": "2026-09-24 18:52:00",
            "event_type": "message",
            "actor_person_id": "wxid_seed06",
            "content": "晚上不想上课怎么办",
            "metadata_json": "{}",
        },
    ]
    msgs = messages_from_rows(rows, {"wxid_seed06": "P6"})
    assert msgs[0].is_revoke is True and msgs[1].is_revoke is False
    now = datetime(2026, 9, 24, 19, 0)
    text = build_chat_window("t@chatroom", messages=msgs, full=40, now=now)
    assert "（系统）P6 撤回了一条消息" in text, text
    assert "sysmsg" not in text and "revokemsg" not in text, text


def test_window_text_separates_pats_from_chat() -> None:
    start = datetime.now().replace(hour=18, minute=50, second=0, microsecond=0)
    rows = [
        WindowMessage(start, "Elaina", '"Elaina" 拍了拍 "鲸鲸"', is_pat=True),
        WindowMessage(
            start + timedelta(minutes=1), "P6", '"P6" 拍了拍 "鲸鲸"', is_pat=True
        ),
        WindowMessage(start + timedelta(minutes=1, seconds=30), "P6", "@鲸鲸 在不在"),
    ]
    text = build_chat_window(
        "t@chatroom", messages=rows, full=40, now=start + timedelta(minutes=5)
    )
    assert "（系统）P6 拍了拍 鲸鲸" in text, text
    assert "（系统）Elaina 拍了拍 鲸鲸" in text, text
    assert '"' not in text, text
    # 只有真正说话的那条带 `P6: ` 前缀（拍一拍不再冒充"她说的话"）
    assert text.count("P6: ") == 1, text
    assert "[18:51] P6: @鲸鲸 在不在" in text, text


def test_older_day_line_hides_pat_quotes() -> None:
    """3 天前的"每天一行"里，起/止若落到拍一拍，也要显示系统样式。"""
    now = datetime(2026, 9, 24, 12, 0, 0)
    rows = [
        WindowMessage(
            datetime(2026, 9, 20, 10, 0), "P6", '"P6" 拍了拍 "鲸鲸"', is_pat=True
        ),
        WindowMessage(datetime(2026, 9, 20, 10, 5), "P6", "晚上不想上课怎么办"),
    ]
    text = build_chat_window("t@chatroom", messages=rows, full=40, now=now)
    assert "（系统）P6 拍了拍 鲸鲸" in text, text
    assert '"' not in text, text


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
