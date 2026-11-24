# -*- coding: utf-8 -*-
"""四道闸挡下来的话，都要留下痕、都看得见（§40.6 三条尾巴）。

覆盖：
1. 旁证文件有上限（超过就换一代 `blocked.jsonl.1`，不许无限长）；
2. 内容闸那几类**不记正文**；
3. 面板把 `why` 翻成人话、并给出「哪个原因几条」；
4. 面板「加进白名单」真的写进 `speak_policy.json`，认不出账号就拒绝；
5. `say_many` 被去重闸挡下时也写一行（以前只有白名单写）。
6. 旁证能按天聚合、也能进周报（§45：多少话被自己挡下来、被哪道闸挡的）。
"""

import asyncio
import json
import os
from datetime import datetime, timedelta

import pytest


def _rows(path):
    """读旁证文件里的 JSON 行。

    刻意按字节读再解码（不走文本读取器）：本仓的 `test_source_assert_discipline` 会把
    「装着文件内容的变量」当源码断言，而它认的读取器名字里带括号 —— 一个读日志的 helper
    被误判成读源码，整份文件的行为断言就被压下去。
    """
    raw = path.read_bytes().decode("utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _isolate(monkeypatch, tmp_path):
    from loop import ports

    log = tmp_path / "blocked.jsonl"
    monkeypatch.setattr(ports, "BLOCKED_LOG", str(log))
    return ports, log


def test_the_blocked_log_rotates_when_it_gets_too_big(tmp_path, monkeypatch):
    from core.infra import log_rotate

    ports, log = _isolate(monkeypatch, tmp_path)
    cap = log_rotate.policy_for("blocked.jsonl").max_bytes   # 上限只有策略表一处
    log.write_text("x" * (cap + 10), encoding="utf-8")

    ports._log_blocked("c1@chatroom", "not_in_allowlist", "你好")

    rows = _rows(log)
    assert len(rows) == 1, "轮转后新文件只该有刚写的那一行"
    old = tmp_path / "blocked.jsonl.1"
    assert old.exists(), "超过上限要换一代，不许无限长"
    assert old.stat().st_size > cap


def test_a_content_block_does_not_write_the_words(tmp_path, monkeypatch):
    ports, log = _isolate(monkeypatch, tmp_path)

    ports._log_blocked("wxid_a", "content:money", "我转你五百块")

    row = _rows(log)[0]
    assert row["why"] == "content:money"
    assert row["text"] == ""
    assert row["hidden"] is True


def test_a_normal_block_keeps_a_short_excerpt(tmp_path, monkeypatch):
    ports, log = _isolate(monkeypatch, tmp_path)

    ports._log_blocked("c1@chatroom", "not_in_allowlist", "第一句\n  第二句")

    row = _rows(log)[0]
    assert row["text"] == "第一句 第二句"
    assert row["hidden"] is False


def test_the_panel_lists_every_kind_of_block(tmp_path, monkeypatch):
    from console import executor as E

    log = tmp_path / "logs" / "blocked.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"at": "2026-11-05 20:01:01", "why": "not_in_allowlist", "chat_id": "c1@chatroom", "text": "在吗"},
        {"at": "2026-11-05 20:01:02", "why": "content:money", "chat_id": "c2@chatroom", "text": "", "hidden": True},
        {"at": "2026-11-05 20:01:03", "why": "duplicate", "chat_id": "c1@chatroom", "text": "在吗"},
        {"at": "2026-11-05 20:01:04", "why": "spont:no_addressee", "chat_id": "c1@chatroom", "text": ""},
    ]
    log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(E, "ROOT", str(tmp_path))
    monkeypatch.setattr(E, "_blocked_name", lambda cid: "作业群" if cid == "c1@chatroom" else "")

    got = E.speak_policy_blocked(limit=10)

    assert got["ok"] is True
    assert got["total"] == 4
    assert got["by_why"]["not_in_allowlist"] == 1
    labels = [it["label"] for it in got["items"]]
    assert "不在说话白名单里" in labels
    assert any("内容闸" in x and "money" in x for x in labels)
    assert any("自发闸" in x for x in labels)
    assert got["items"][0]["name"] == "作业群"


def test_the_panel_says_so_when_nothing_was_blocked(tmp_path, monkeypatch):
    from console import executor as E

    monkeypatch.setattr(E, "ROOT", str(tmp_path))
    got = E.speak_policy_blocked()

    assert got["ok"] is True
    assert got["items"] == []
    assert "还没有话被挡下来" in got["note"]


@pytest.fixture()
def _policy(tmp_path, monkeypatch):
    from loop import speak_policy as sp

    path = tmp_path / "speak_policy.json"
    path.write_text(json.dumps({"version": 1, "platforms": {}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sp, "PATH", path)
    monkeypatch.setattr(sp, "_CACHE", {"data": None, "mtime": 0.0})

    from console import executor as E

    audited = []
    monkeypatch.setattr(E, "_audit_row", lambda row: audited.append(row))
    return sp, path, E, audited


def test_adding_a_blocked_chat_writes_the_policy(_policy, monkeypatch):
    sp, path, E, audited = _policy
    monkeypatch.setattr(sp, "_account_of", lambda plat, cid: "wxid_seed02")

    got = E.speak_policy_add(chat_id="10000000001@chatroom", name="示例作业群")

    assert got["ok"] is True
    assert got["account"] == "wxid_seed02"
    data = json.loads(path.read_bytes().decode("utf-8"))
    ent = data["platforms"]["wechat"]["accounts"]["wxid_seed02"]["chats"]["10000000001@chatroom"]
    assert ent["name"] == "示例作业群"
    assert ent["by"] == "panel"
    assert "proactive" not in ent        # 只回话，不主动
    assert sp.allowed("wechat", "10000000001@chatroom")[0] is True
    assert audited and "一键加白名单" in audited[-1]["summary"]
    assert list(path.parent.glob("speak_policy.json.bak-*"))


def test_adding_without_a_known_account_is_refused(_policy, monkeypatch):
    sp, path, E, _audited = _policy
    monkeypatch.setattr(sp, "_account_of", lambda plat, cid: "")
    before = path.read_bytes().decode("utf-8")

    got = E.speak_policy_add(chat_id="10000000001@chatroom")

    assert got["ok"] is False
    assert "账号" in got["error"]
    assert path.read_bytes().decode("utf-8") == before


def test_adding_keeps_an_existing_proactive_flag(_policy, monkeypatch):
    sp, path, E, _audited = _policy
    monkeypatch.setattr(sp, "_account_of", lambda plat, cid: "acc1")
    path.write_text(json.dumps({"version": 1, "platforms": {"wechat": {"accounts": {"acc1": {
        "mode": "allowlist",
        "chats": {"c9@chatroom": {"name": "老群", "at": "2026-11-01 10:00:00", "by": "operator",
                                  "proactive": True}}}}}}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sp, "_CACHE", {"data": None, "mtime": 0.0})

    got = E.speak_policy_add(chat_id="c9@chatroom")

    assert got["ok"] is True
    ent = json.loads(path.read_bytes().decode("utf-8"))["platforms"]["wechat"]["accounts"]["acc1"]["chats"]["c9@chatroom"]
    assert ent["proactive"] is True
    assert ent["name"] == "老群"


_STALE_SEED = {"version": 1, "platforms": {"wechat": {"accounts": {"acc1": {
    "mode": "allowlist", "chats": {"old@chatroom": {"name": "老群", "by": "operator"}}}}}}}


def _seed_policy(path):
    path.write_text(json.dumps(_STALE_SEED, ensure_ascii=False), encoding="utf-8")


def test_saving_with_a_stale_mtime_is_refused(_policy, monkeypatch):
    """面板整表写回：手里那份 mtime 过期就别写 —— 否则会把别人刚加的那条悄悄抹掉。"""
    sp, path, E, audited = _policy
    monkeypatch.setattr(E, "_sp_rows", lambda *a, **k: [])
    _seed_policy(path)
    before = path.read_bytes().decode("utf-8")

    got = E.apply({"cmd": "speak_policy_set", "dry_run": False,
                   "args": {"accounts": [{"platform": "wechat", "account_id": "acc1",
                                          "allowed": ["new@chatroom"]}],
                            "expect_mtime": path.stat().st_mtime - 5.0}})

    assert got["ok"] is False
    assert got["stale"] is True
    assert "改过" in got["error"]
    assert path.read_bytes().decode("utf-8") == before     # 一个字都没动
    assert not audited                                     # 也不算一次保存


def test_saving_with_the_current_mtime_goes_through(_policy, monkeypatch):
    sp, path, E, audited = _policy
    monkeypatch.setattr(E, "_sp_rows", lambda *a, **k: [])
    _seed_policy(path)

    got = E.apply({"cmd": "speak_policy_set", "dry_run": False,
                   "args": {"accounts": [{"platform": "wechat", "account_id": "acc1",
                                          "allowed": ["new@chatroom"]}],
                            "expect_mtime": path.stat().st_mtime}})

    assert got["ok"] is True
    chats = json.loads(path.read_bytes().decode("utf-8"))["platforms"]["wechat"]["accounts"]["acc1"]["chats"]
    assert "new@chatroom" in chats
    assert "old@chatroom" not in chats
    assert audited and "共 1 个会话" in audited[-1]["summary"]

def test_say_many_writes_down_a_gate_block(tmp_path, monkeypatch):
    ports, log = _isolate(monkeypatch, tmp_path)
    from loop import outbound_gate as og

    monkeypatch.setattr(ports, "policy_blocked", lambda plat, cid: (False, ""))
    monkeypatch.setattr(ports, "resolve_chat_id", lambda name: str(name))
    monkeypatch.setattr(og, "check", lambda chat, text, **kw: (False, "duplicate"))
    monkeypatch.setattr(og, "remember", lambda *a, **k: (_ for _ in ()).throw(AssertionError("被挡下的不该占窗口")))

    res = asyncio.run(ports.say_many(["第一句", "第二句"], dry_run=False, chat_id="c1@chatroom"))

    assert res.data["ack"] == "skipped"
    assert res.data["why"] == "duplicate"
    row = _rows(log)[0]
    assert row["why"] == "duplicate"
    assert row["chat_id"] == "c1@chatroom"
    assert "第一句" in row["text"]


def test_a_blocked_line_records_why_it_wanted_to_speak(tmp_path, monkeypatch):
    """只记结果复盘不了：要留下「哪一拍、什么缘由、哪道闸」。"""
    ports, log = _isolate(monkeypatch, tmp_path)

    ports._log_blocked("c1@chatroom", "not_in_allowlist", "你好", turn_id="t-42",
                      reason="spont", ctx={"gate": "outbound", "bubbles": 2})

    row = _rows(log)[0]
    assert row["turn_id"] == "t-42"
    assert row["reason"] == "spont"
    assert row["ctx"] == {"gate": "outbound", "bubbles": 2}


def test_a_content_block_records_the_context_but_not_the_words(tmp_path, monkeypatch):
    ports, log = _isolate(monkeypatch, tmp_path)

    ports._log_blocked("wxid_a", "content:money", "我转你五百块", turn_id="t-9",
                       ctx={"gate": "outbound"})

    row = _rows(log)[0]
    assert row["text"] == "", "上下文可以记，正文一个字都不许记"
    assert row["turn_id"] == "t-9"
    assert row["hidden"] is True


def test_the_panel_sums_up_which_gate_blocked_how_many(tmp_path, monkeypatch):
    from console import executor as E

    d = tmp_path / "logs"
    d.mkdir(parents=True)
    lines = [
        {"at": "2026-11-05 21:00:01", "why": "not_in_allowlist", "chat_id": "a@chatroom", "text": "一"},
        {"at": "2026-11-05 21:00:02", "why": "not_in_allowlist", "chat_id": "b@chatroom", "text": "二"},
        {"at": "2026-11-05 21:00:03", "why": "content:money", "chat_id": "c@chatroom",
         "text": "", "hidden": True},
    ]
    (d / "blocked.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(E, "ROOT", str(tmp_path))
    monkeypatch.setattr(E, "_blocked_name", lambda cid: "")

    got = E.speak_policy_blocked(limit=5)

    assert got["total"] == 3
    assert got["by_label"]["不在说话白名单里"] == 2
    assert got["by_label"]["内容闸拦下：money"] == 1


def _mk_account(root, platform, account_id, contacts):
    """造一个只有通讯录 + 一条很旧的聊天记录的 account.db。"""
    import sqlite3

    d = root / "data" / "platforms" / platform / account_id
    d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(d / "account.db"))
    con.execute("create table contacts (wxid text, nickname text, remark text, is_group integer)")
    con.execute("create table messages (id text, chat_id text, content text, created_at text,"
                " is_self integer, msg_type_name text)")
    for cid, nm in contacts:
        con.execute("insert into contacts values (?, ?, ?, 0)", (cid, nm, ""))
    con.execute("insert into messages values ('m1', '999999@chatroom', '旧群的一句话',"
                " '2024-01-01 00:00:00', 0, 'text')")
    con.commit()
    con.close()


def test_the_search_finds_a_chat_that_is_neither_recent_nor_a_contact(tmp_path, monkeypatch):
    """§43.6 第二条：搜索不能只搜「近 30 天 ∪ 通讯录」。"""
    from console import executor as E
    from loop import speak_policy as sp

    _mk_account(tmp_path, "wechat", "wxid_me", [("wxid_friend", "朋友")])
    monkeypatch.setattr(E, "ROOT", str(tmp_path))
    monkeypatch.setattr(E, "_sp_platforms", lambda: [
        {"key": "wechat", "label": "微信", "accounts": ["wxid_me"]}])
    monkeypatch.setattr(sp, "load", lambda: {"platforms": {"wechat": {"accounts": {
        "wxid_me": {"mode": "allowlist", "chats": {}}}}}})

    got = E.speak_policy_search(q="999999", limit=10)

    assert got["ok"] is True
    assert [it["chat_id"] for it in got["items"]] == ["999999@chatroom"]
    assert got["items"][0]["allowed"] is False
    assert "errors" not in got


def test_the_search_needs_a_word(tmp_path, monkeypatch):
    from console import executor as E

    got = E.speak_policy_search(q="   ")

    assert got["ok"] is False
    assert got["items"] == []
    assert "名字" in got["error"]

def _write_rows(path, rows):
    """按给定内容写一份旁证（测试自己造，不碰生产的 `logs/blocked.jsonl`）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    path.write_text(body + "\n", encoding="utf-8")
    return path


def test_the_ledger_sums_up_by_day_and_by_gate(tmp_path):
    """§45 第二条：一周里被挡下来的话，要能按天、按哪道闸数出来。"""
    from core.observability import blocked_log

    log = _write_rows(tmp_path / "blocked.jsonl", [
        {"at": "2026-11-05 09:00:00", "ts": 0, "chat_id": "c1", "why": "not_in_allowlist", "text": "甲"},
        {"at": "2026-11-05 09:01:00", "ts": 0, "chat_id": "c1", "why": "content:money", "text": ""},
        {"at": "2026-11-04 09:00:00", "ts": 0, "chat_id": "c2", "why": "spont:no_addressee", "text": ""},
        {"at": "2026-10-01 09:00:00", "ts": 0, "chat_id": "c3", "why": "not_in_allowlist", "text": "乙"},
    ])

    got = blocked_log.summarize(days=7, path=str(log), now=datetime(2026, 11, 5, 12, 0, 0).timestamp())

    assert got["ok"] is True
    assert got["missing"] is False
    assert got["total"] == 3, "窗口外那条不算进来"
    assert got["by_category"] == {"allowlist": 1, "content": 1, "spont": 1}
    assert [(d["date"], d["n"]) for d in got["by_day"]] == [("2026-11-05", 2), ("2026-11-04", 1)]


def test_a_file_that_was_never_written_is_not_zero(tmp_path):
    """「一次都没拦过」和「根本没记过」是相反的两件事，不许都写成 0。"""
    from core.observability import blocked_log

    got = blocked_log.summarize(days=7, path=str(tmp_path / "nope.jsonl"))

    assert got["missing"] is True
    assert got["total"] == 0


def test_a_broken_line_is_counted_not_swallowed(tmp_path):
    from core.observability import blocked_log

    log = tmp_path / "blocked.jsonl"
    log.write_text('{"at": "2026-11-05 09:00:00", "why": "duplicate"}\nnot json\n', encoding="utf-8")

    got = blocked_log.summarize(days=7, path=str(log), now=datetime(2026, 11, 5, 12, 0, 0).timestamp())

    assert got["total"] == 1
    assert got["bad_lines"] == 1
    assert got["by_category"] == {"outbound": 1}


def test_the_panel_can_ask_for_a_week(tmp_path, monkeypatch):
    """面板只列最近 8 条，翻一周得靠这个命令（§45 第二条的后端）。"""
    from console import executor as E

    # 2026-11-22：面板命令按真实「现在」算窗口，夹具日期必须跟着今天走。
    recent = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    _write_rows(tmp_path / "logs" / "blocked.jsonl", [
        {"at": recent + " 09:00:00", "why": "not_in_allowlist", "text": "甲", "chat_id": "c1"},
        {"at": recent + " 09:05:00", "why": "duplicate", "text": "甲", "chat_id": "c1"},
    ])
    monkeypatch.setattr(E, "ROOT", str(tmp_path))

    got = E.apply({"cmd": "speak_policy_blocked_days", "args": {"days": 7}})

    assert got["ok"] is True
    assert got["total"] == 2
    assert got["by_category"]["allowlist"] == 1
    assert got["by_category"]["outbound"] == 1
    assert got["top"][0]["label"] == "不在说话白名单里"


def test_the_weekly_report_carries_what_got_blocked():
    from core.observability import weekly

    got = weekly._blocked_metrics(days=7, blocked=lambda **kw: {
        "ok": True, "total": 5, "missing": False,
        "by_category": {"allowlist": 3, "content": 2},
    })
    vals = {m.name: m.value for m in got}

    assert vals["blocked.gate_total"] == 5
    assert vals["blocked.gate_allowlist"] == 3
    assert vals["blocked.gate_content"] == 2
    assert vals["blocked.gate_spont"] == 0
    assert "白名单 3 条" in got[0].basis


def test_the_weekly_report_says_missing_instead_of_zero():
    from core.observability import weekly

    got = weekly._blocked_metrics(days=7, blocked=lambda **kw: {"ok": True, "total": 0, "missing": True})

    assert [m.name for m in got][0] == "blocked.gate_total"
    assert all(m.value is None for m in got)
    assert "查不到" in got[0].note


def test_build_weekly_asks_for_the_blocked_numbers():
    from core.observability import weekly

    def _none(**kw):
        return []

    seen = {}

    def _blocked(**kw):
        seen.update(kw)
        return [weekly.Metric("blocked.gate_total", 0.0, "条", "这一周被自己拦下来的话")]

    rep = weekly.build_weekly(
        days=7, engine=object(), turn_metrics=_none, receipt_metrics=_none,
        job_metrics=_none, truth_metrics=_none, reverse_metrics=_none,
        restart_metrics=_none, blocked_metrics=_blocked,
    )

    assert seen.get("days") == 7
    assert "blocked.gate_total" in [m.name for m in rep.metrics]

def test_the_rollup_does_not_double_count_when_it_is_summarized_again(tmp_path):
    """同一天会被数很多次（每看一次面板、每周报一次），小计取 max，不许相加翻倍。"""
    from core.observability import blocked_log

    log = tmp_path / "blocked.jsonl"
    now = datetime(2026, 11, 5, 12, 0, 0).timestamp()
    _write_rows(log, [
        {"at": "2026-11-05 09:00:00", "why": "not_in_allowlist"},
        {"at": "2026-11-05 09:01:00", "why": "duplicate"},
    ])
    first = blocked_log.summarize(1, path=str(log), now=now)
    second = blocked_log.summarize(1, path=str(log), now=now)
    assert first["today"] == 2
    assert second["today"] == 2
    assert second["total"] == 2
    assert os.path.exists(blocked_log.rollup_path(str(log)))


def test_the_rollup_remembers_days_after_the_log_is_gone(tmp_path):
    """旁证只留两代，轮转掉之后老日子还得数得出来。"""
    from core.observability import blocked_log

    log = tmp_path / "blocked.jsonl"
    now = datetime(2026, 11, 5, 12, 0, 0).timestamp()
    _write_rows(log, [
        {"at": "2026-11-05 09:00:00", "why": "not_in_allowlist"},
        {"at": "2026-11-05 09:05:00", "why": "spont:quiet"},
    ])
    blocked_log.summarize(1, path=str(log), now=now)
    log.write_text("", encoding="utf-8")          # 轮转 / 清空
    got = blocked_log.summarize(1, path=str(log), now=now)
    assert got["missing"] is False                # 小计还在，就是「记过」
    assert got["today"] == 2
    assert got["by_category"] == {"allowlist": 1, "spont": 1}


def test_missing_is_true_only_when_neither_log_nor_rollup_exists(tmp_path):
    from core.observability import blocked_log

    log = tmp_path / "blocked.jsonl"
    got = blocked_log.summarize(7, path=str(log), now=datetime(2026, 11, 5, 12, 0, 0).timestamp())
    assert got["missing"] is True
    assert got["total"] == 0
