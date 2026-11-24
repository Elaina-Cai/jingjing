"""检索上沉（批 3b）：反复命中的词条 → 提案，夹具排除，术语归一。

归属：`core/quality/memory_sinking.py` 的纯函数（`collect_candidates` /
`normalize_term` / `is_fixture`）与开关。设计
`docs/群聊理解修复_批3设计.md` §4。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# 数据库隔离（照 tests/test_event_chain_links.py / test_command_service_db.py）：
# 本文件会经 core.bootstrap.init_singletons() 触发 init_meta_db + run_migrations
# （bootstrap 兜底、runtime 访问器等），不隔离就把 pending 迁移落到**真实**
# meta / account 库。2026-09-29 实测：Migration 51 被本套测试落到了生产库
# （data/yinyue_meta.db + 4 个账号库，applied_at 14:58:19Z）。必须在 import core.*
# 之前设好：PathSettings 在 core.config.settings **首次**导入时就取值。
# ---------------------------------------------------------------------------
_ISO_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "_test_memory_sinking_iso"
)
os.makedirs(_ISO_ROOT, exist_ok=True)
os.environ["META_DB_PATH"] = os.path.join(_ISO_ROOT, "meta.db")
os.environ["PLATFORMS_DIR"] = os.path.join(_ISO_ROOT, "platforms")
os.environ["DATA_DIR"] = os.path.join(_ISO_ROOT, "data")

from core.config.settings import config as _config  # noqa: E402

# 守卫：env 只在 core.config.settings 首次导入前设好才有效，否则真实路径已经
# 固化进 config。这里硬失败，绝不静默把迁移/写操作落到生产库。
assert os.path.realpath(_config.paths.meta_db_path) != os.path.realpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "yinyue_meta.db"
    )
), f"测试数据库隔离失效：meta_db_path={_config.paths.meta_db_path}"


from core.quality.memory_sinking import (  # noqa: E402
    MAX_TERM_CHARS,
    MIN_OCCURRENCES,
    MIN_SCORE,
    collect_candidates,
    is_fixture,
    normalize_term,
    sink_candidates,
    sinking_enabled,
)


def _rec(chat="10000000001@chatroom", query="fkxqs", score=0.8, mid="proj:real1",
         bot="wxid_seed02", at: str | None = None):
    return {
        # time-bomb fix: dynamic now (was a hardcoded 14-day-old date)
        "at": at or datetime.now().replace(microsecond=0).isoformat(),
        "chat_id": chat,
        "platform": "wechat",
        "bot_account_id": bot,
        "query": query,
        "hits": [{"memory_id": mid, "score": score}],
    }


# ------------------------------------------------------------------ normalize
def test_normalize_term() -> None:
    # 真正的词条：短黑话 / 术语
    assert normalize_term("fkxqs") == "fkxqs"
    assert normalize_term("  fkxqs ？") == "fkxqs"
    assert normalize_term("yyds") == "yyds"
    assert normalize_term("青协") == "青协"
    # 空 / 太长 → 不是词条
    assert normalize_term("") == ""
    assert normalize_term("   ") == ""
    assert normalize_term("这是一句很长的完整句子不适合当词条") == ""


def test_media_placeholders_are_not_terms() -> None:
    from core.quality.memory_sinking import normalize_term
    for word in ("图片", "表情包", "视频", "语音", "文件"):
        assert normalize_term(word) == "", word


def test_normalize_rejects_sentences_and_commands() -> None:
    """红线：句子/命令/评测探针绝不能被当成「群内术语」（实测全是误报）。"""
    # 以 @ 开头 = 对她说的话，不是术语
    assert normalize_term("@鲸鲸帮我看看这个") == ""
    assert normalize_term("@鲸鲸总结一下") == ""
    assert normalize_term("@鲸鲸看看图") == ""
    assert normalize_term("@鲸鲸你记得我说过什么吗") == ""
    assert normalize_term("@鲸鲸我是学委吗") == ""
    # 含句子标记（我/你/吗/帮我/不对…）
    assert normalize_term("不对不是周五是周六") == ""
    assert normalize_term("不对我没说过这个") == ""
    assert normalize_term("别聊这个了") == ""
    assert normalize_term("修好了吗手机") == ""
    assert normalize_term("第一行第二行第三行") == ""
    assert normalize_term("Elaina拍了拍我") == ""
    # 审计实测误报（2026-09-23）：寒暄句 / 拍一拍事件文本
    assert normalize_term("在干嘛呀") == ""
    assert normalize_term("P5拍了拍鲸鲸") == ""
    assert normalize_term("XXX撤回了一条消息") == ""


# ------------------------------------------------------------------ fixtures
def test_is_fixture() -> None:
    """§5 数据坑：trace 无测试隔离，owner_private / bot_a / proj:lex* / 12345678 是夹具。"""
    assert is_fixture(_rec(chat="owner_private", bot="bot_a", mid="proj:lex0")) is True
    assert is_fixture(_rec(chat="owner_raw", bot="bot_a")) is True
    assert is_fixture(_rec(bot="bot_a")) is True
    assert is_fixture(_rec(mid="proj:semantic")) is True
    assert is_fixture(_rec(mid="proj:lexical")) is True
    # 合成群号（评测夹具）也是假的
    assert is_fixture(_rec(chat="12345678@chatroom")) is True
    # 真实流量放行
    assert is_fixture(_rec()) is False
    assert is_fixture(_rec(mid="proj:4776abf22d954431bf8cc534318c750e")) is False


# ------------------------------------------------------------------ candidates
def test_repeated_queries_become_candidates() -> None:
    records = [_rec(query="fkxqs") for _ in range(3)] + [_rec(query="yyds")]
    out = collect_candidates(records)
    assert len(out) == 1
    assert out[0]["term"] == "fkxqs"
    assert out[0]["occurrences"] == 3
    assert out[0]["chat_id"] == "10000000001@chatroom"


def test_below_threshold_is_not_a_candidate() -> None:
    assert collect_candidates([_rec() for _ in range(MIN_OCCURRENCES - 1)]) == []


def test_weak_hits_are_ignored() -> None:
    """命中分太低 = 检索本来就没找到东西，沉淀无从谈起。"""
    records = [_rec(score=MIN_SCORE - 0.1) for _ in range(5)]
    assert collect_candidates(records) == []


def test_fixture_records_are_excluded() -> None:
    """夹具记录再多也不该产出候选（否则会用测试污染去改她的知识）。"""
    records = [_rec(chat="owner_private", bot="bot_a", mid="proj:lex0") for _ in range(10)]
    assert collect_candidates(records) == []


def test_private_chat_is_not_a_lexicon_source() -> None:
    """术语只服务群聊：私聊的重复检索不产候选（设计 §4.1 边界）。"""
    records = [_rec(chat="wxid_friend") for _ in range(6)]
    assert collect_candidates(records) == []


def test_event_texts_never_become_candidates() -> None:
    """拍一拍/撤回等事件文本即使反复命中也不上沉（红线）。"""
    records = [_rec(query="P5拍了拍鲸鲸") for _ in range(6)]
    assert collect_candidates(records) == []


def test_candidates_are_per_chat() -> None:
    records = [_rec(chat="a@chatroom") for _ in range(3)] + [
        _rec(chat="b@chatroom") for _ in range(3)
    ]
    out = collect_candidates(records)
    assert {c["chat_id"] for c in out} == {"a@chatroom", "b@chatroom"}


def test_candidates_sorted_by_occurrences() -> None:
    records = [_rec(query="aa") for _ in range(3)] + [_rec(query="bb") for _ in range(6)]
    out = collect_candidates(records)
    assert [c["term"] for c in out] == ["bb", "aa"]


def test_memory_ids_collected_and_deduped() -> None:
    records = [_rec(mid="proj:real1"), _rec(mid="proj:real1"), _rec(mid="proj:real2")]
    out = collect_candidates(records)
    assert sorted(out[0]["memory_ids"]) == ["proj:real1", "proj:real2"]


# ------------------------------------------------------------------ flag & io
def test_sinking_flag_defaults_off() -> None:
    os.environ.pop("YINYUE_FEATURE_MEMORY_SINKING", None)
    assert sinking_enabled() is False
    os.environ["YINYUE_FEATURE_MEMORY_SINKING"] = "1"
    assert sinking_enabled() is True
    os.environ.pop("YINYUE_FEATURE_MEMORY_SINKING", None)


def test_sink_candidates_is_gated_by_flag() -> None:
    """开关关时哪怕是真实重复也不产候选（只读审计也走它）。"""
    import json
    from pathlib import Path

    # 本沙箱对 tempfile 目录禁止写文件（PermissionError），改用项目内可写目录；
    # 干净环境同样适用。用完即删。
    tmp = Path("output") / "_test_tmp_sinking"
    tmp.mkdir(parents=True, exist_ok=True)
    trace = tmp / "trace_gated.jsonl"
    try:
        trace.write_text(
            "\n".join(json.dumps(_rec()) for _ in range(5)), encoding="utf-8"
        )
        os.environ.pop("YINYUE_FEATURE_MEMORY_SINKING", None)
        assert sink_candidates(path=trace) == []
        os.environ["YINYUE_FEATURE_MEMORY_SINKING"] = "1"
        got = sink_candidates(path=trace)
        assert len(got) == 1 and got[0]["term"] == "fkxqs"
        os.environ.pop("YINYUE_FEATURE_MEMORY_SINKING", None)
    finally:
        try:
            trace.unlink()
        except OSError:
            pass


def test_load_records_tolerates_missing_file() -> None:
    from pathlib import Path

    from core.quality.memory_sinking import load_records

    assert load_records(path=Path("C:/nonexistent/nope.jsonl")) == []


def test_load_records_handles_utc_offset_timestamps() -> None:
    """回归：trace 的 at 带 +00:00 偏移，与 naive 墙钟比较会 TypeError 被静默吞掉
    → 恒返回 0 条（真实踩过的坑）。必须能读进来。"""
    import json
    from pathlib import Path

    from core.quality.memory_sinking import load_records

    import datetime as _dt

    rec = _rec()
    # 不能用写死的日期：`load_records(days=30)` 是按现在往前推的窗口，
    # 写死的时间过一个月就自己衰出窗口（2026-10-24 真的这样红过一次）。
    rec["at"] = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).isoformat()
    tmp = Path("output") / "_test_tmp_sinking"
    tmp.mkdir(parents=True, exist_ok=True)
    trace = tmp / "trace_offset.jsonl"
    try:
        trace.write_text(json.dumps(rec) + "\n", encoding="utf-8")
        got = load_records(days=30, path=trace)
    finally:
        try:
            trace.unlink()
        except OSError:
            pass
    assert len(got) == 1, got
    assert got[0]["query"] == "fkxqs"


def test_parse_at_normalizes_offset() -> None:
    from core.quality.memory_sinking import _parse_at

    parsed = _parse_at("2026-09-23T15:30:00+00:00")
    assert parsed is not None and parsed.tzinfo is None  # 已转成 naive 本地
    assert _parse_at("") is None
    assert _parse_at("not-a-date") is None


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
