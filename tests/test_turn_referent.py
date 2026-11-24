"""TurnReferent checks: the turn's referent must be extracted, never guessed.

Run directly: python tests/test_turn_referent.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
# 隔离前导（照 tests/test_reminder_mapping.py:34-41）：本文件只用纯函数，但保持同一
# 纪律，万一以后加了走 bootstrap 的断言也不会碰到生产库。
_TEST_LOG_DIR = str(Path("data") / "_test_logs_turn_referent")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_DIR", _TEST_LOG_DIR)
_ISO_ROOT = (ROOT / "data" / "_turn_referent_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("META_DB_PATH", str(_ISO_ROOT / "meta.db"))
os.environ.setdefault("PLATFORMS_DIR", str(_ISO_ROOT / "platforms"))
os.environ.setdefault("DATA_DIR", str(_ISO_ROOT / "data"))

from core.context.chat_window import WindowMessage  # noqa: E402
from core.context.referent import (  # noqa: E402
    KIND_IMAGE,
    KIND_STICKER,
    ROLE_ATTACHED,
    ROLE_CURRENT,
    ROLE_QUOTED,
    bind_pronouns,
    referent_from_message,
    referent_from_text,
    render_pronoun_facts,
)

STICKER_QUOTED = "afd8b92ddf249d6a90b8a327663e9882"  # P5发、被引用的那张
STICKER_SELF = "e302850b0ddc59100a3ce39ead1f7de3"    # 鲸鲸自己发的那张


def _row(
    speaker: str,
    content: str,
    *,
    at: datetime,
    msg_id: str = "",
    is_self: bool = False,
    is_pat: bool = False,
) -> WindowMessage:
    """一行可见消息（与 chat_window/admission 同一份 rows 形制）。"""
    return WindowMessage(
        occurred_at=at, speaker=speaker, content=content,
        is_self=is_self, is_pat=is_pat, msg_id=msg_id,
    )


def _incident_rows() -> list[WindowMessage]:
    """2026-09-30 18:34:05 那轮的真实形状：Elaina 连拍 4 人（没拍 P3），
    鲸鲸插了一句，P3 随后说「@鲸鲸 对啊对啊，他在霸凌我」——「他」= Elaina。
    """
    at = datetime(2026, 9, 30, 18, 33, 0)
    rows = [
        _row("Elaina", '"Elaina" 拍了拍 "P6"', at=at, msg_id="m1001", is_pat=True),
        _row("P6", "哈哈哈", at=at + timedelta(seconds=4), msg_id="m1002"),
        _row("Elaina", '"Elaina" 拍了拍 "P5"', at=at + timedelta(seconds=8),
             msg_id="m1003", is_pat=True),
        _row("Elaina", '"Elaina" 拍了拍 "P7"', at=at + timedelta(seconds=12),
             msg_id="m1004", is_pat=True),
        _row("Elaina", '"Elaina" 拍了拍 "P4"', at=at + timedelta(seconds=16),
             msg_id="m1005", is_pat=True),
        _row("鲸鲸", "怎么了这是", at=at + timedelta(seconds=20), msg_id="m1006",
             is_self=True),
        _row("P3", "@鲸鲸 对啊对啊，他在霸凌我", at=at + timedelta(seconds=25),
             msg_id="m1007"),
    ]
    return rows


INCIDENT_TEXT = "@鲸鲸 对啊对啊，他在霸凌我"



def test_quoted_sticker_from_message() -> bool:
    print("\n[引用表情包 -> quoted 锚]")
    msg = {
        "chat_id": "c1",
        "msg_id": "m900",
        "sender_id": "u1",
        "sender_name": "Elaina",
        "is_self": 0,
        "quote_reply_id": "m800",
        "content": (
            "[引用 P5: wxid_seed05:1790224425316:0:"
            + STICKER_QUOTED + "::0] 什么东西"
        ),
    }
    ref = referent_from_message(msg, account_id="bot1")
    quoted = ref.quoted_media
    checks = [
        ("quoted 只有一条", len(quoted) == 1),
        ("md5 是引用那张", bool(quoted) and quoted[0].md5 == STICKER_QUOTED),
        ("kind=sticker", bool(quoted) and quoted[0].kind == KIND_STICKER),
        ("作者归给P5", bool(quoted) and quoted[0].sender_name == "P5"),
        ("primary 指向 quoted",
         ref.primary_media is not None and ref.primary_media.role == ROLE_QUOTED),
        ("quoted_message_id 保留", ref.quoted_message_id == "m800"),
        ("自己发的不是这条", not ref.is_self),
    ]
    return _report(checks)


def test_quoted_media_path_from_message() -> bool:
    print("\n[引用图片 -> path 锚]")
    msg = {
        "chat_id": "c1",
        "msg_id": "m901",
        "sender_id": "u1",
        "quote_reply_id": "m801",
        "_quoted_media_path": r"C:\cache\quoted.jpg",
        "content": "[引用 P5: [图片]] 这张是什么",
    }
    ref = referent_from_message(msg, account_id="bot1")
    quoted = ref.quoted_media
    checks = [
        ("quoted 只有一条", len(quoted) == 1),
        ("path 是引用那张", bool(quoted) and quoted[0].path == r"C:\cache\quoted.jpg"),
        ("没有 md5 时 kind=image", bool(quoted) and quoted[0].kind == KIND_IMAGE),
        ("primary 是它",
         ref.primary_media is not None and ref.primary_media.path == r"C:\cache\quoted.jpg"),
    ]
    return _report(checks)


def test_current_sticker_and_self_flag() -> bool:
    print("\n[当前消息就是表情包 -> current + is_self]")
    msg = {
        "chat_id": "c1",
        "msg_id": "m902",
        "sender_id": "bot1",
        "sender_name": "鲸鲸",
        "is_self": 1,
        "content": f"[表情包:{STICKER_SELF}]",
        "_attached_sticker_id": STICKER_SELF,
    }
    ref = referent_from_message(msg, account_id="bot1")
    current = [m for m in ref.media if m.role == ROLE_CURRENT]
    checks = [
        ("current 一条", len(current) == 1),
        ("md5 正确", bool(current) and current[0].md5 == STICKER_SELF),
        ("is_self 标上", bool(current) and current[0].is_self),
        ("回合自己也标上", ref.is_self),
        ("没有误挂 quoted", not ref.quoted_media),
    ]
    return _report(checks)


def test_text_md5_belongs_to_quote_not_current() -> bool:
    print("\n[文本里的 md5 属于引用，不是本条自己]")
    msg = {
        "chat_id": "c1",
        "msg_id": "m903",
        "sender_id": "u1",
        "content": f"[引用 P5: foo:{STICKER_QUOTED}::0] 看看这个",
    }
    ref = referent_from_message(msg, account_id="bot1")
    roles = [m.role for m in ref.media]
    checks = [
        ("quoted 在", ROLE_QUOTED in roles),
        ("current 不在", ROLE_CURRENT not in roles),
        ("只有一条", len(ref.media) == 1),
    ]
    return _report(checks)


def test_attached_paths_role() -> bool:
    print("\n[批次补齐的相邻媒体 -> attached]")
    msg = {
        "chat_id": "c1",
        "msg_id": "m904",
        "sender_id": "u1",
        "content": "看看",
        "_attached_media_paths": ["a.jpg", "b.jpg"],
    }
    ref = referent_from_message(msg, account_id="bot1")
    attached = [m for m in ref.media if m.role == ROLE_ATTACHED]
    checks = [
        ("attached 两条", len(attached) == 2),
        ("路径保留", [m.path for m in attached] == ["a.jpg", "b.jpg"]),
        ("attached 优先于无", ref.primary_media is not None),
    ]
    return _report(checks)


def test_from_text_and_json_safe() -> bool:
    print("\n[群触发路径：从纯文本建锚 + JSON 安全]")
    ref = referent_from_text(
        f"[表情包:{STICKER_SELF}]",
        chat_id="c1",
        message_id="m905",
        sender_id="u2",
        sender_name="风语",
    )
    current = [m for m in ref.media if m.role == ROLE_CURRENT]
    dumped = json.dumps(ref.as_dict(), ensure_ascii=False)
    checks = [
        ("current 一条", len(current) == 1),
        ("md5 正确", bool(current) and current[0].md5 == STICKER_SELF),
        ("sender 归给发的人", bool(current) and current[0].sender_id == "u2"),
        ("as_dict 可 JSON", STICKER_SELF in dumped and "quoted" in dumped),
    ]
    return _report(checks)


def test_pronoun_binding_real_incident() -> bool:
    print("\n[真实回归 2026-09-30 18:34：P3 说的「他」= Elaina]")
    rows = _incident_rows()
    bindings = bind_pronouns(rows, speaker="P3", text=INCIDENT_TEXT)
    hit = bindings[0] if bindings else None
    rendered = render_pronoun_facts(bindings, speaker="P3")
    checks = [
        ("恰好一条绑定", len(bindings) == 1),
        ("代词是「他」", hit is not None and hit.pronoun == "他"),
        ("绑到 Elaina", hit is not None and hit.referent_name == "Elaina"),
        ("不得绑到鲸鲸", hit is not None and hit.referent_name != "鲸鲸"),
        ("理由是动作证据", hit is not None and hit.reason == "action_evidence"),
        ("证据带 msg_id", hit is not None and bool(hit.evidence_msg_id)),
        ("证据里点了被拍的人", hit is not None and "P6" in hit.evidence),
        ("事实行含 Elaina", "Elaina" in rendered),
        ("事实行是 [指代事实]", rendered.startswith("[指代事实]\n- 指代事实：")),
        ("事实行劝她别对号入座", "对号入座" in rendered),
    ]
    return _report(checks)


def test_pronoun_binding_unknown_when_no_candidate() -> bool:
    print("\n[无候选（只有系统行/她自己）→ 未知，不猜]")
    at = datetime(2026, 9, 30, 18, 34, 0)
    rows = [
        _row("鲸鲸", "我在呢", at=at, msg_id="m2001", is_self=True),
        _row("系统", "（系统）某人撤回了一条消息", at=at, msg_id="m2002"),
    ]
    bindings = bind_pronouns(rows, speaker="P3", text="他到底想干嘛")
    rendered = render_pronoun_facts(bindings, speaker="P3")
    hit = bindings[0] if bindings else None
    checks = [
        ("有一条绑定", len(bindings) == 1),
        ("referent 空 = 未知", hit is not None and hit.referent_name == ""),
        ("resolved=False", hit is not None and not hit.resolved),
        ("理由=no_candidate", hit is not None and hit.reason == "no_candidate"),
        ("文案写「不明」", "不明" in rendered),
        ("文案要求先问", "先问" in rendered),
        ("文案禁止认领", "不要认领" in rendered),
    ]
    return _report(checks)


def test_pronoun_binding_tie_is_unknown() -> bool:
    print("\n[两个同等候选 → 未知]")
    at = datetime(2026, 9, 30, 18, 34, 0)
    rows = [
        _row("Elaina", '"Elaina" 拍了拍 "P6"', at=at, msg_id="m3001", is_pat=True),
        _row("P6", '"P6" 拍了拍 "Elaina"', at=at + timedelta(seconds=5),
             msg_id="m3002", is_pat=True),
    ]
    bindings = bind_pronouns(rows, speaker="P3", text="他在干嘛")
    hit = bindings[0] if bindings else None
    rendered = render_pronoun_facts(bindings, speaker="P3")
    checks = [
        ("有一条绑定", len(bindings) == 1),
        ("不硬选一个", hit is not None and hit.referent_name == ""),
        ("理由=ambiguous", hit is not None and hit.reason == "ambiguous"),
        ("文案写「不明」", "不明" in rendered),
        ("文案列出多人", "Elaina" in rendered and "P6" in rendered),
    ]
    return _report(checks)


def test_pronoun_binding_self_exception_is_narrow() -> bool:
    print("\n[自身窄例外：没点名/@/引用鲸鲸时绝不绑自己，也不硬绑别人]")
    rows = _incident_rows()
    no_pronoun = bind_pronouns(rows, speaker="P3", text="@鲸鲸 你拍我干嘛")
    accused = bind_pronouns(
        rows, speaker="P3", text="@鲸鲸 你拍我干嘛，他是不是在笑我"
    )
    quoted = bind_pronouns(
        [_row("鲸鲸", "好", at=datetime(2026, 9, 30, 18, 0), msg_id="m4001",
              is_self=True)],
        speaker="P5",
        text="[引用 鲸鲸: 好] 他为什么要回复我",
    )
    plain = bind_pronouns(rows, speaker="P3", text="今天几号")
    others = bind_pronouns(rows, speaker="P3", text="其他人都别说话了")
    indirect = bind_pronouns(
        rows, speaker="P3", text="@鲸鲸 你说他打我，是不是你挑的"
    )
    accused_hit = accused[0] if accused else None
    quoted_hit = quoted[0] if quoted else None
    indirect_hit = indirect[0] if indirect else None
    checks = [
        ("「你拍我干嘛」无第三人称代词 → 不注入", no_pronoun == []),
        ("指控收话人（=她）→ 绑鲸鲸", accused_hit is not None
         and accused_hit.referent_name == "鲸鲸"),
        ("指控场景不得绑到 Elaina", accused_hit is not None
         and accused_hit.referent_name != "Elaina"),
        ("理由是 addressee_accusation", accused_hit is not None
         and accused_hit.reason == "addressee_accusation"),
        ("「你说他打我」是转述 → 不算指控她", indirect_hit is not None
         and indirect_hit.referent_name != "鲸鲸"),
        ("引用她本人 → 绑鲸鲸（保留窄语义）", quoted_hit is not None
         and quoted_hit.referent_name == "鲸鲸"),
        ("没有代词 → 空列表", plain == []),
        ("「其他人」不是代词 → 空列表", others == []),
    ]
    return _report(checks)


def _report(checks: list[tuple[str, bool]]) -> bool:
    ok = True
    for name, good in checks:
        ok &= bool(good)
        print(f"  {'✓' if good else '✗'} {name}")
    return bool(ok)


def main() -> bool:
    print("=" * 60)
    print("turn referent checks")
    print("=" * 60)
    tests = [
        test_quoted_sticker_from_message,
        test_quoted_media_path_from_message,
        test_current_sticker_and_self_flag,
        test_text_md5_belongs_to_quote_not_current,
        test_attached_paths_role,
        test_from_text_and_json_safe,
        test_pronoun_binding_real_incident,
        test_pronoun_binding_unknown_when_no_candidate,
        test_pronoun_binding_tie_is_unknown,
        test_pronoun_binding_self_exception_is_narrow,
    ]
    results = []
    for test in tests:
        try:
            results.append((test.__name__, bool(test())))
        except Exception:  # pragma: no cover - diagnostics only
            import traceback

            traceback.print_exc()
            results.append((test.__name__, False))
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    print(f"{passed}/{len(results)} passed")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
