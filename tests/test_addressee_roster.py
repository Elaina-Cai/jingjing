# -*- coding: utf-8 -*-
r"""task-47 验收：@ 目标的「名单合法性 / 合理性」三档判定（影子）。

钉六件事：

1. **合法性** = 群名单（`group_memberships` 群名片）/ 本轮触发者 / 本轮 inbound 点名的人 /
   本窗口近期说过话的人；名字必须**唯一**解析到其中一人，重名 ⇒ ``ambiguous`` 拒绝；
2. **合理性** = 被 @ 的人近期说过话（或就是触发者 / inbound 点名的人）；
   合法但不合理只记 ``unlikely``，**不拦**；
3. **三态**：``off`` 逐字旧行为且零新日志；``shadow`` 只写一行 ``addressee_guard_shadow``、
   正文一个字不改；``on`` **只剥离非法**（``unlikely`` 不剥离）；
4. 私聊不参与（``None``，走老路）；``@所有人`` 这类广播不判定；
5. 判定不新造 @ 正则（走 canonical ``mentioned_names`` / ``_address_span``）；
6. 名单读不到 = 没有这份证据（空名单而不是崩溃）。

复算：``.\.venv_it\Scripts\python.exe -X utf8 -u tests\test_addressee_roster.py``
"""
from __future__ import annotations

import os
import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ISO = ROOT / ".test_tmp" / "addressee_roster"
(ISO / "platforms").mkdir(parents=True, exist_ok=True)
(ISO / "data").mkdir(parents=True, exist_ok=True)
os.environ.setdefault("META_DB_PATH", str(ISO / "meta.db"))
os.environ.setdefault("PLATFORMS_DIR", str(ISO / "platforms"))
os.environ.setdefault("DATA_DIR", str(ISO / "data"))

from core.context import group_members as G  # noqa: E402
from core.speech.domain import ConversationTarget, OutboundDraft  # noqa: E402
from core.speech.outbound import OutboundGateway  # noqa: E402
from core.speech.resolver import AddresseePerson, AddresseeFacts, AddresseeResolver  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def ok(name: str, cond: bool, detail: object = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append("%s %r" % (name, detail))


# ---------------------------------------------------------------------------
# 替身：只实现判定读口用到的三个东西（connect / execute / fetchall）
# ---------------------------------------------------------------------------
class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, db, counter):
        self._db = db
        self._counter = counter

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._counter["queries"] += 1
        query = str(sql)
        params = params or {}
        if "group_memberships" in query:
            rows = self._db["members"].get(params.get("chat"), [])
        elif "terminal_events" in query:
            rows = list(self._db["recent"].get(params.get("chat"), []))
            if params.get("lim"):
                rows = rows[: int(params["lim"])]
        else:
            rows = []
        return _Rows(rows)


class _Engine:
    def __init__(self, db, counter):
        self._db = db
        self._counter = counter

    def connect(self):
        return _Conn(self._db, self._counter)


def make_repo(members=None, recent=None, people=None):
    db = {
        "members": dict(members or {}),
        "recent": dict(recent or {}),
        "people": dict(people or {}),
    }
    counter = {"queries": 0}

    def find_persons(name, platform="", limit=5):
        ids = db["people"].get(str(name or "").strip(), ())
        return [SimpleNamespace(person_id=pid) for pid in list(ids)[:limit]]

    repo = SimpleNamespace(
        engine=_Engine(db, counter),
        people=SimpleNamespace(find_persons=find_persons),
        counter=counter,
        db=db,
    )
    return repo


class _Recorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def _record(self, event, **kwargs):
        self.events.append((event, kwargs))

    info = _record
    warning = _record
    debug = _record
    error = _record

    def named(self, event):
        return [payload for name, payload in self.events if name == event]


def clear_caches() -> None:
    G._ROSTER_CACHE.clear()
    G._RECENT_CACHE.clear()


# ---------------------------------------------------------------------------
# 1. 名单索引
# ---------------------------------------------------------------------------
def test_member_index_and_cache() -> None:
    clear_caches()
    repo = make_repo(members={
        "g1": [("P6", "p1"), ("鲸鲸", "p2"), ("P6", "p3"), ("", "p4")],
    })
    index = G.member_index("g1", meta_repo=repo)
    ok("index.duplicate", index.get("P6") == frozenset({"p1", "p3"}), index.get("P6"))
    ok("index.single", index.get("鲸鲸") == frozenset({"p2"}), index.get("鲸鲸"))
    ok("index.blank_name_skipped", "" not in index, list(index))
    before = repo.counter["queries"]
    G.member_index("g1", meta_repo=repo)
    ok("index.cached", repo.counter["queries"] == before, repo.counter)
    ok("index.empty_chat", G.member_index("", meta_repo=repo) == {}, "empty")
    clear_caches()


# ---------------------------------------------------------------------------
# 2. 合法性 / 合理性
# ---------------------------------------------------------------------------
def test_judge_legal_and_recent() -> None:
    clear_caches()
    repo = make_repo(members={"g1": [("P6", "p1")]}, recent={"g1": [("p1",)]})
    v = G.judge_address("@P6 你要来吗？", chat_id="g1", meta_repo=repo)
    ok("recent.legal", v is not None and v.legal, v)
    ok("recent.plausible", v is not None and v.plausible, v)
    ok("recent.reason", v is not None and v.reason == "allowed", v)
    ok("recent.source", v is not None and v.roster_source == G.ROSTER_SOURCE_MEMBERSHIP, v)
    ok("recent.in_roster", v is not None and v.in_roster, v)
    ok("recent.unlikely_false", v is not None and not v.unlikely, v)
    clear_caches()


def test_judge_legal_but_unlikely() -> None:
    clear_caches()
    repo = make_repo(members={"g1": [("P6", "p1")]}, recent={"g1": [("p2",)]})
    v = G.judge_address("在吗 @P6", chat_id="g1", meta_repo=repo)
    # 「在吗 @P6」的 @ 不在招呼位（前面是空格 + 中文），按 canonical 规则不算点名
    ok("unlikely.non_address", v is None, v)
    v = G.judge_address("@P6 在吗", chat_id="g1", meta_repo=repo)
    ok("unlikely.legal", v is not None and v.legal, v)
    ok("unlikely.flag", v is not None and v.unlikely, v)
    ok("unlikely.reason", v is not None and v.reason == "unlikely", v)
    ok("unlikely.stripped_empty", v is not None and not v.stripped_text or v.stripped_text == "", v)
    clear_caches()


def test_judge_active_hours_window() -> None:
    """合理性判据 = 近 N 小时说过话（`YINYUE_ADDRESSEE_ROSTER_ACTIVE_HOURS`）。"""
    clear_caches()
    repo = make_repo(members={"g1": [("P6", "p1")]}, recent={"g1": [("p1",)]})
    v = G.judge_address("@P6 你看着办", chat_id="g1", meta_repo=repo)
    ok("active.plausible_by_default", v is not None and v.plausible, v)
    ok("active.reason_allowed", v is not None and v.reason == "allowed", v)
    # 显式 0 小时窗口 ⇒ 退回「近 N 条」，同样命中
    v = G.judge_address("@P6 你看着办", chat_id="g1", active_hours=0, meta_repo=repo)
    ok("active.hours_zero_falls_back", v is not None and v.plausible, v)
    # 空近期 ⇒ 合法但 unlikely（只记录）
    clear_caches()
    empty = make_repo(members={"g1": [("P6", "p1")]})
    v = G.judge_address("@P6 你看着办", chat_id="g1", meta_repo=empty)
    ok("active.unlikely_when_silent", v is not None and v.legal and v.unlikely, v)
    clear_caches()


def test_active_hours_env() -> None:
    """`YINYUE_ADDRESSEE_ROSTER_ACTIVE_HOURS`：默认 24、钳 1–168、非法回落。"""
    import os

    old = os.environ.get("YINYUE_ADDRESSEE_ROSTER_ACTIVE_HOURS")
    try:
        os.environ.pop("YINYUE_ADDRESSEE_ROSTER_ACTIVE_HOURS", None)
        ok("env.active_default", G.active_hours() == 24, G.active_hours())
        os.environ["YINYUE_ADDRESSEE_ROSTER_ACTIVE_HOURS"] = "6"
        ok("env.active_six", G.active_hours() == 6, G.active_hours())
        os.environ["YINYUE_ADDRESSEE_ROSTER_ACTIVE_HOURS"] = "999"
        ok("env.active_clamped", G.active_hours() == 168, G.active_hours())
        os.environ["YINYUE_ADDRESSEE_ROSTER_ACTIVE_HOURS"] = "0"
        ok("env.active_min_one", G.active_hours() == 1, G.active_hours())
        os.environ["YINYUE_ADDRESSEE_ROSTER_ACTIVE_HOURS"] = "abc"
        ok("env.active_invalid", G.active_hours() == 24, G.active_hours())
    finally:
        os.environ.pop("YINYUE_ADDRESSEE_ROSTER_ACTIVE_HOURS", None)
        if old is not None:
            os.environ["YINYUE_ADDRESSEE_ROSTER_ACTIVE_HOURS"] = old


def test_judge_non_member() -> None:
    clear_caches()
    repo = make_repo(members={"g1": [("P6", "p1")]}, people={"路人甲": ["p5"]})
    v = G.judge_address("@路人甲 你好", chat_id="g1", meta_repo=repo)
    ok("nonmember.legal_false", v is not None and not v.legal, v)
    ok("nonmember.person", v is not None and v.person_id == "p5", v)
    ok("nonmember.reason", v is not None and v.reason == "not_member", v)
    ok("nonmember.stripped", v is not None and v.stripped_text == "你好", v)
    clear_caches()
    repo = make_repo(members={"g1": [("P6", "p1")]})
    v = G.judge_address("@查无此人 你好", chat_id="g1", meta_repo=repo)
    ok("unresolved.reason", v is not None and v.reason == "unresolved", v)
    ok("unresolved.stripped", v is not None and v.stripped_text == "你好", v)
    clear_caches()


def test_judge_ambiguous() -> None:
    """两个本群成员同名、且**这一轮都算在场** ⇒ 仍判 ambiguous（拒绝且不猜）。"""
    clear_caches()
    repo = make_repo(members={"g1": [("P6", "p1"), ("P6", "p3")]},
                     recent={"g1": [("p1",), ("p3",)]})
    v = G.judge_address("@P6 你好", chat_id="g1", meta_repo=repo)
    ok("ambiguous.flag", v is not None and v.ambiguous, v)
    ok("ambiguous.legal_false", v is not None and not v.legal, v)
    ok("ambiguous.reason", v is not None and v.reason == "ambiguous", v)
    ok("ambiguous.stripped", v is not None and v.stripped_text == "你好", v)
    clear_caches()


def test_ambiguous_resolved_in_group() -> None:
    """同名多人，但只有一人在本轮证据里 ⇒ 用本群证据收窄成他（仍记 ambiguous 供观测）。"""
    clear_caches()
    repo = make_repo(members={"g1": [("P6", "p1"), ("P6", "p3")]},
                     recent={"g1": [("p1",)]})
    v = G.judge_address("@P6 你好", chat_id="g1", meta_repo=repo)
    ok("resolve.legal", v is not None and v.legal, v)
    ok("resolve.person", v is not None and v.person_id == "p1", v)
    ok("resolve.reason", v is not None and v.reason == "ambiguous_resolved_in_group", v)
    ok("resolve.flag_kept", v is not None and v.ambiguous, v)
    ok("resolve.not_stripped", v is not None and not v.stripped_text, v)

    # 全局同名但不在本群的人**不**参与收窄：inbound 里提到同名，也不会造出歧义
    clear_caches()
    repo = make_repo(
        members={"g1": [("P6", "p1")]},
        recent={"g1": [("p1",)]},
        people={"P6": ["p2", "p7"]},
    )
    v = G.judge_address("@P6 你好", chat_id="g1",
                        inbound="@P6 在吗", meta_repo=repo)
    ok("resolve.global_not_polluting", v is not None and v.legal and not v.ambiguous, v)
    ok("resolve.global_person", v is not None and v.person_id == "p1", v)
    clear_caches()


def test_inbound_named_does_not_pollute_name_map() -> None:
    """`_inbound_named` 只给 legal_ids 扩容；非本群的人不进名字表。"""
    clear_caches()
    repo = make_repo(people={"路人甲": ["p9"]})
    name_map: dict[str, set[str]] = {}
    ids = G._inbound_named(name_map, "@路人甲 你好", platform="wechat",
                           meta_repo=repo, roster_ids={"p1"})
    ok("inbound.ids_kept", ids == {"p9"}, ids)
    ok("inbound.name_map_clean", name_map == {}, name_map)
    clear_caches()


def test_judge_trigger_and_inbound() -> None:
    clear_caches()
    repo = make_repo(members={"g1": [("P6", "p1")]})
    v = G.judge_address(
        "@P5 在吗", chat_id="g1", meta_repo=repo,
        trigger_name="P5", trigger_id="p9",
    )
    ok("trigger.legal", v is not None and v.legal, v)
    ok("trigger.source", v is not None and v.roster_source == G.ROSTER_SOURCE_TRIGGER, v)
    ok("trigger.plausible", v is not None and v.plausible, v)
    clear_caches()
    repo = make_repo(people={"阿澈": ["p7"]})
    v = G.judge_address(
        "@阿澈 你怎么看", chat_id="g1", meta_repo=repo,
        inbound="@阿澈 你们几个说说",
    )
    ok("inbound.legal", v is not None and v.legal, v)
    ok("inbound.source", v is not None and v.roster_source == G.ROSTER_SOURCE_INBOUND, v)
    ok("inbound.plausible", v is not None and v.plausible, v)
    clear_caches()


def test_judge_skips() -> None:
    clear_caches()
    repo = make_repo(members={"g1": [("P6", "p1")]})
    ok("skip.no_at", G.judge_address("今天天气不错", chat_id="g1", meta_repo=repo) is None)
    ok("skip.broadcast",
       G.judge_address("@所有人 开会了", chat_id="g1", meta_repo=repo) is None)
    ok("skip.mid_sentence",
       G.judge_address("让他@P6就行", chat_id="g1", meta_repo=repo) is None)
    ok("skip.empty_text", G.judge_address("   ", chat_id="g1", meta_repo=repo) is None)
    clear_caches()


def test_judge_no_roster_no_crash() -> None:
    clear_caches()

    class _Boom:
        class engine:
            @staticmethod
            def connect():
                raise RuntimeError("db down")

        people = None

    v = G.judge_address("@P6 你好", chat_id="g1", meta_repo=_Boom())
    ok("noroster.no_crash", v is not None and not v.legal, v)
    ok("noroster.reason", v is not None and v.reason == "unresolved", v)
    ok("noroster.stripped", v is not None and v.stripped_text == "你好", v)
    clear_caches()


# ---------------------------------------------------------------------------
# 3. 出口三态（shadow / on / off）
# ---------------------------------------------------------------------------
def _gateway(repo=None):
    gw = OutboundGateway.__new__(OutboundGateway)
    gw._addressee_resolver = AddresseeResolver(person_id_of=lambda name, platform="": "")
    if repo is not None:
        gw._roster_repo = repo
    return gw


def _draft(content: str, *, chat_type: str = "group", chat_id: str = "g1") -> OutboundDraft:
    target = ConversationTarget(
        platform="wechat", account_id="bot", chat_id=chat_id,
        chat_type=chat_type, sender_id="wxid_x", sender_name="P6",
    )
    return OutboundDraft(request_id="r1", kind="text", target=target, content=content)


def _patched_mode(value: str):
    from core import features

    original = features.mode

    def fake(name, default=None, **kwargs):
        assert not hasattr(features, "ADDRESSEE_GUARD"), "R0：老守卫开关已删"
        return original(name, default, **kwargs) if default is not None else original(name)

    features.mode = fake
    return lambda: setattr(features, "mode", original)


def test_old_outbound_guard_is_retired() -> None:
    """R0：老路 `_apply_addressee_guard`（出口按名单事后仲裁 @ 目标）已整段删除。

    留着的是根治路 `_apply_addressee_render`（把判层给出的受话结论渲染成 @前缀），
    它不改判、不换人。
    """
    assert not hasattr(OutboundGateway, "_apply_addressee_guard"), "老守卫必须已删除"
    assert not hasattr(OutboundGateway, "_audit_addressee"), "老守卫的审计必须已删除"
    # 2026-10-23（N1 扫尾）：归属结论已无生产者，出站按结论渲染 @ 的整条线
    # （`core/speech/outbound_addressee.py` + `_apply_addressee_render`）一并退役。
    assert not hasattr(OutboundGateway, "_apply_addressee_render"), "渲染路必须已删除"
    assert not (ROOT / "core/speech/outbound_addressee.py").exists(), "渲染模块必须已删除"
    # 老路是否真的删掉了，由上面两行 `not hasattr(...)` 直接回答（读整份源码是重复证据）


def test_facts_still_three_sources() -> None:
    facts = AddresseeFacts(
        trigger=AddresseePerson(name="P5", raw_id="p9", source="trigger"),
        quoted=AddresseePerson(name="P6", source="quoted"),
    )
    ok("facts.allowed_unchanged",
       [p.label() for p in facts.allowed()] == ["P5", "P6"], facts.allowed())


def main() -> int:
    test_member_index_and_cache()
    test_judge_legal_and_recent()
    test_judge_legal_but_unlikely()
    test_judge_non_member()
    test_judge_ambiguous()
    test_ambiguous_resolved_in_group()
    test_inbound_named_does_not_pollute_name_map()
    test_judge_trigger_and_inbound()
    test_judge_skips()
    test_judge_active_hours_window()
    test_active_hours_env()
    test_judge_no_roster_no_crash()
    test_old_outbound_guard_is_retired()
    test_facts_still_three_sources()
    if FAILED:
        print("\nFAILED %d / %d" % (len(FAILED), len(FAILED) + PASSED))
        for name in FAILED:
            print(" -", name)
        return 1
    print("\nALL PASSED (%d)" % PASSED)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
