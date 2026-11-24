# -*- coding: utf-8 -*-
"""写侧 ①³ 随手记（B 路线确定性观察器）：开关三态 / 只落日志 / span 可回原文 / 0-3 上限。

覆盖：
- flag 已登记（tri、默认 off、env_key 正确），模块只读 ``features.mode``（不是 enabled）；
- ``off``：不写任何日志、无副作用；``shadow``：只落 ``worth_remembering_shadow`` 一行；
- span 可回原文（``source[start:end] == quote``，``span_ref()`` / ``to_record()`` 同区间）；
- 每轮 0-3 条、**允许 0 条**（纯闲聊不硬记）；确定性（同输入同输出）；
- 六类 kind 各抓一个样本（``bare`` 默认不抓，打开 ``include_bare`` 才出）（``bare`` 默认不抓，打开 ``include_bare`` 才出）；纯度：只 import stdlib/structlog/core，不调 LLM、不碰库。
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("LOG_DIR", str(ROOT / ".test_tmp" / "task12" / "logs"))
os.environ.setdefault("META_DB_PATH", str(ROOT / ".test_tmp" / "task12" / "meta.db"))
os.environ.setdefault("PLATFORMS_DIR", str(ROOT / ".test_tmp" / "task12" / "platforms"))
os.environ.setdefault("DATA_DIR", str(ROOT / ".test_tmp" / "task12" / "data"))

from core import features  # noqa: E402
from core.contracts.events import EvidenceRef  # noqa: E402
from core.memory import note_writer as nw  # noqa: E402

ENV = "YINYUE_FEATURE_WORTH_REMEMBERING"
SOURCE_PATH = ROOT / "core" / "memory" / "note_writer.py"


class _Env:
    """临时改环境变量并在退出时恢复（照 tests/test_intent_layer.py 的写法）。"""

    def __init__(self, **kv):
        self.kv = kv
        self.old: dict[str, str | None] = {}

    def __enter__(self):
        for key, value in self.kv.items():
            self.old[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        return self

    def __exit__(self, *exc):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


class _FakeLogger:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def info(self, event, **fields):
        self.calls.append((event, fields))


def _source() -> str:
    return SOURCE_PATH.read_text(encoding="utf-8")


def test_flag_registered_tri_default_off():
    assert features.WORTH_REMEMBERING == "worth_remembering"
    spec = features.FLAG_REGISTRY[features.WORTH_REMEMBERING]
    assert spec.kind == "tri"
    assert spec.default == features.MODE_OFF
    assert features.env_key(features.WORTH_REMEMBERING) == ENV
    source = _source()
    assert "features.mode(" in source
    assert "features.WORTH_REMEMBERING" in source
    with _Env(**{ENV: None}):
        assert nw.worth_remembering_mode() == nw.MODE_OFF
        assert nw.worth_remembering_shadow() is False
        assert nw.worth_remembering_enabled() is False


def test_off_mode_is_silent():
    logger = _FakeLogger()
    notes = nw.observe_message("记得明天交物理作业.pdf", message_id="m1", chat_id="c1")
    assert notes
    with _Env(**{ENV: None}):
        payload = nw.shadow_record(notes, chat_id="c1", message_id="m1", logger=logger)
    assert logger.calls == []
    assert payload["chat_id"] == "c1" and payload["message_id"] == "m1"
    assert payload["n"] == len(notes) and len(payload["notes"]) == len(notes)
    assert payload["notes"][0]["span"]["message_id"] == "m1"


def test_shadow_logs_only():
    logger = _FakeLogger()
    notes = nw.observe_message(
        "物理作业.pdf 明天9月22日交", message_id="m2", chat_id="c2", speaker="Elaina"
    )
    assert notes
    with _Env(**{ENV: "shadow"}):
        assert nw.worth_remembering_shadow() is True
        payload = nw.shadow_record(notes, chat_id="c2", message_id="m2", logger=logger)
    assert len(logger.calls) == 1
    event, fields = logger.calls[0]
    assert event == "worth_remembering_shadow"
    assert fields["chat_id"] == "c2" and fields["message_id"] == "m2"
    assert fields["n"] == len(notes)
    assert fields["kinds"] == [c.kind for c in notes]
    assert fields["spans"] == [(c.start, c.end) for c in notes]
    assert payload["notes"] == [c.to_record() for c in notes]


def test_span_points_back_to_source():
    text = "选课通知：物理作业.pdf 9月22日交，实验报告8080808080"
    notes = nw.observe_message(
        text, message_id="m3", speaker="Elaina", at="2026-10-16T12:00:00Z", chat_id="c3"
    )
    assert notes
    for cand in notes:
        assert text[cand.start : cand.end] == cand.quote
        assert cand.note_text == cand.quote
        ref = cand.span_ref()
        assert isinstance(ref, EvidenceRef)
        assert (ref.message_id, ref.chat_id, ref.start, ref.end) == (
            "m3",
            "c3",
            cand.start,
            cand.end,
        )
        assert ref.quoted_text == cand.quote[: nw.QUOTE_MAX]
        assert cand.speaker == "Elaina" and cand.at.startswith("2026-10-16")
        assert cand.to_record()["span"] == {
            "message_id": "m3",
            "start": cand.start,
            "end": cand.end,
            "quote": cand.quote,
        }


def test_zero_notes_allowed_and_cap_three():
    assert nw.observe_message("哈哈，这也行") == ()
    assert nw.observe_message("在吗") == ()
    assert nw.observe_message("在吗", include_bare=True)[0].kind == nw.KIND_BARE
    text = (
        "物理作业.pdf 大学物理实验.zip 双臂桥测低值电阻.pdf "
        "9月22日 10月15日 8080808080 247991621 282000708 记得明天带伞 我来自外地"
    )
    notes = nw.observe_message(text)
    assert 0 < len(notes) <= nw.NOTE_MAX == 3
    assert nw.observe_message(text, limit=0) == ()
    assert len(nw.observe_message(text, limit=2)) <= 2


def test_observe_turn_caps_across_rows():
    rows = [
        {
            "event_id": "e1",
            "content": "物理作业.pdf 记得明天带伞",
            "name": "Elaina",
            "occurred_at": "2026-10-16T12:00:00Z",
        },
        {"event_id": "e2", "content": "编号 8080808080", "name": "Elaina", "occurred_at": "2026-10-16T12:01:00Z"},
    ]
    notes = nw.observe_turn(rows, chat_id="c9")
    assert 0 < len(notes) <= nw.NOTE_MAX
    assert notes[0].chat_id == "c9" and notes[0].message_id in ("e1", "e2")
    assert notes[0].speaker == "Elaina"
    assert nw.observe_turn(rows, limit=0) == ()


def test_deterministic_and_kind_coverage():
    cases = (
        ("物理作业.pdf", nw.KIND_FILE),
        ("9月22日交作业", nw.KIND_DATE),
        ("编号 8080808080", nw.KIND_NUMBER),
        ("记得明天带伞", nw.KIND_PROMISE),
        ("我来自外地", nw.KIND_PLACE),
        ("@某某1234567", nw.KIND_MENTION),
    )
    for text, kind in cases:
        notes = nw.observe_message(text)
        assert notes, text
        assert notes[0].kind == kind, (text, notes[0].kind)
        assert nw.observe_message(text) == notes
    bare = nw.observe_message("选课通知", include_bare=True)
    assert bare and bare[0].kind == nw.KIND_BARE
    assert nw.observe_message("选课通知", include_bare=True) == bare


def test_kind_priority_prefers_unrecoverable_tokens():
    notes = nw.observe_message("选课通知 物理作业.pdf", include_bare=True)
    assert notes and notes[0].kind == nw.KIND_FILE
    assert notes[0].quote == "物理作业.pdf"


def test_env_tri_state_is_read_each_call():
    with _Env(**{ENV: None}):
        assert nw.worth_remembering_mode() == nw.MODE_OFF
    with _Env(**{ENV: "shadow"}):
        assert nw.worth_remembering_mode() == nw.MODE_SHADOW
        assert nw.worth_remembering_shadow() is True
    with _Env(**{ENV: "on"}):
        assert nw.worth_remembering_mode() == nw.MODE_ON
        assert nw.worth_remembering_enabled() is True
    with _Env(**{ENV: "ON"}):
        assert nw.worth_remembering_mode() == nw.MODE_ON
    with _Env(**{ENV: "weird"}):
        assert nw.worth_remembering_mode() == nw.MODE_OFF
        assert nw.worth_remembering_mode(nw.MODE_ON) == nw.MODE_ON


def test_noise_guards_media_and_false_promises():
    """媒体占位/引用里的数字串不算事实；「谁给我补」不是承诺；bare 默认不抓。"""
    assert nw.observe_message("[表情包:afd8b92ddf249d6a90b8a327663e9882]") == ()
    assert nw.observe_message("[图片]") == ()
    assert nw.observe_message("[引用 🦎336249558: 在吗]") == ()
    # @昵称里的平台 id 不是事实（旧行为：把这些 QQ 号当 number 记了）
    mention = nw.observe_message("@代郡324674695 哦？是叫我吗？")
    assert [c.kind for c in mention] == [nw.KIND_MENTION], mention
    assert mention[0].quote == "324674695"
    assert [c.kind for c in nw.observe_message("@AAA要饭的白白公主177290505 拍冬天呢")] == [nw.KIND_MENTION]
    # 「我记得…」「喊我干嘛」「答应的事一定办到」不是承诺（旧正则会误报）
    assert nw.observe_message("我记得，就是刚才被「学委」这俩字绕晕了") == ()
    assert nw.observe_message("@Elaina 在呢，喊我干嘛？") == ()
    assert nw.observe_message("气死我了，我今天上午又重买了一套。") == ()
    assert nw.observe_message("偶dps33.3w，孩子们我缺的双手剑新枝这块谁给我补】 https://w") == ()
    assert nw.observe_message("选课通知") == ()


def test_purity_imports_and_no_db():
    source = _source()
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    # hashlib：note_id = 内容哈希（task-15 回填可复现要求），仍只依赖标准库。
    assert modules <= {"__future__", "re", "hashlib", "dataclasses", "typing", "structlog", "core"}, modules
    assert not (modules & {"sqlite3", "sqlalchemy", "subprocess", "socket", "llm"})
    lowered = source.lower()
    for banned in ("insert into", "core.bootstrap", "requests.", "httpx."):
        assert banned not in lowered, banned


def test_noise_guards_url_and_fragment_promises():
    """链接路径里的数字、索要式「给我X」、疑问句尾都不算随手记。"""
    url = "@所有人 请24、25、26级学委转发 https://jwb.ggdupt.edu.cn/info/1053/10766.htm"
    assert nw.observe_message(url) == (), [(n.kind, n.quote) for n in nw.observe_message(url)]
    linked = nw.observe_message("看这个 https://example.com/report.pdf 谢谢")
    assert [(n.kind, n.quote) for n in linked] == [("file", "report.pdf")], linked
    for noisy in ("给我钱", "给我体力合了", "你下次回来的时候不就能喝了吗"):
        got = nw.observe_message(noisy)
        assert got == (), (noisy, [(n.kind, n.quote) for n in got])
    ok = nw.observe_message("等你们写完物理实验报告发我一份")
    assert [n.kind for n in ok] == ["promise"], ok




def test_place_url_and_year_rules() -> None:
    cases = {
        "宿舍全滚蛋了，除了我重庆人体质，其他人全羊了": [("place", "重庆")],
        "@鲸鲸 帮我看看广州今天的天气": [("place", "广州")],
        "@鲸鲸 帮我看看后天南宁的天气": [("place", "南宁")],
        "@P6 七小时而已，坐着就到了，到杭州别急着赶路，先找碗片儿川填填肚子。": [("place", "杭州")],
        "看这个 https://www.bilibili.com/video/BV1V9p16VEHR/?share 好活": [("number", "BV1V9p16VEHR")],
        "@所有人 请在规定时间内选择课程学习@关于2026-2027学年第一学期线上通识教育": [("number", "2026-2027")],
        "今天天气不错，出去走走": [],
        "我是年轻人，我精神好": [],
        "学校食堂的天气无所谓": [],
    }
    for text, expect in cases.items():
        got = [(n.kind, n.quote) for n in nw.observe_message(text)]
        assert got == expect, (text, got, expect)

def test_base_offset_shifts_spans():
    """content_start 非 0 时 span 记绝对偏移；默认 0 逐字不变。"""
    text = "2026年10月20日陪奶奶去长沙，电话 13800138000"
    plain = nw.observe_message(text, message_id="m1")
    shifted = nw.observe_message(text, message_id="m1", base_offset=40)
    assert plain, "样本应抓到至少一条候选"
    assert [(c.start, c.end) for c in shifted] == [(c.start + 40, c.end + 40) for c in plain]
    assert [c.quote for c in shifted] == [c.quote for c in plain]
    assert all(text[c.start - 40 : c.end - 40] == c.quote for c in shifted)
    assert nw.content_base({"content_start": 12}) == 12
    assert nw.content_base({"content_start": -1}) == 0
    assert nw.content_base({}) == 0
    assert nw.content_base(None) == 0



TESTS = [
    test_flag_registered_tri_default_off,
    test_off_mode_is_silent,
    test_shadow_logs_only,
    test_span_points_back_to_source,
    test_zero_notes_allowed_and_cap_three,
    test_observe_turn_caps_across_rows,
    test_deterministic_and_kind_coverage,
    test_kind_priority_prefers_unrecoverable_tokens,
    test_noise_guards_media_and_false_promises,
    test_noise_guards_url_and_fragment_promises,
    test_env_tri_state_is_read_each_call,
    test_purity_imports_and_no_db,
    test_place_url_and_year_rules,
    test_base_offset_shifts_spans,
]


def main() -> bool:
    ok = True
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
            ok = False
        else:
            print(f"PASS {test.__name__}")
    if ok:
        print(f"note_writer checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
