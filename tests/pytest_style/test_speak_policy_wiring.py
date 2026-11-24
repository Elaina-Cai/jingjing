"""说话白名单必须长在「回话」那条路上（2026-11-05 修）。

事故：控制台「律」里没勾的群（示例宿舍群 `10000000002@chatroom`）她照样回话。
根因：`speak_policy.allowed()` 只装在 `ports.say()` 里，而模型回话走
`ports.say_many()` 的「整组一次入队」快路（直接 `world_db.enqueue`）。
"""
from __future__ import annotations

import asyncio

import pytest

from loop import ports, speak_policy


@pytest.fixture(autouse=True)
def _blocked_log_isolated(tmp_path, monkeypatch):
    """被拦下的句子会真的写进 `logs/blocked.jsonl`（生产旁证）。

    2026-11-05 踩过：跑一遍测试，生产那份日志里就多了两条「你好」「排练用的」。
    这一条把路径钉到 tmp，谁也别往生产账本里写。
    """
    monkeypatch.setattr(ports, "BLOCKED_LOG", str(tmp_path / "blocked.jsonl"))
    return tmp_path / "blocked.jsonl"


def _recorder(monkeypatch):
    sent = []

    def _enqueue(gid, chat, items, **kw):
        sent.append({"gid": gid, "chat": chat, "items": items, "kw": kw})
        return [7, 8][: len(items)]

    from db import world_db

    monkeypatch.setattr(world_db, "enqueue", _enqueue)
    monkeypatch.setattr(ports, "resolve_chat_id", lambda c: str(c or ""))
    return sent


def _policy(monkeypatch, result):
    def _allowed(platform, chat_id):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(speak_policy, "allowed", _allowed)


def test_say_many_group_path_is_blocked_by_the_whitelist(monkeypatch):
    sent = _recorder(monkeypatch)
    _policy(monkeypatch, (False, "not_in_allowlist"))
    res = asyncio.run(ports.say_many(["第一句", "第二句"], dry_run=False,
                                     chat_id="10000000002@chatroom"))
    assert res.ok is False
    assert "not_in_allowlist" in res.error
    assert res.data.get("code") == "not_in_allowlist"
    assert sent == []


def test_say_single_path_is_blocked_too(monkeypatch):
    sent = _recorder(monkeypatch)
    _policy(monkeypatch, (False, "unknown_chat"))
    res = asyncio.run(ports.say("你好", dry_run=False, chat_id="10000000002@chatroom"))
    assert res.ok is False and "unknown_chat" in res.error
    assert sent == []


def test_an_allowlisted_chat_still_sends(monkeypatch):
    sent = _recorder(monkeypatch)
    _policy(monkeypatch, (True, "in_allowlist"))
    res = asyncio.run(ports.say_many(["我去问一下场地"], dry_run=False, chat_id="10000000004@chatroom"))
    assert res.ok is True
    assert res.data.get("ack") == "queued"
    assert len(sent) == 1 and sent[0]["chat"] == "10000000004@chatroom"


def test_a_broken_policy_blocks_instead_of_leaking(monkeypatch):
    sent = _recorder(monkeypatch)
    _policy(monkeypatch, RuntimeError("policy file unreadable"))
    res = asyncio.run(ports.say_many(["这句话"], dry_run=False, chat_id="10000000004@chatroom"))
    assert res.ok is False
    assert "policy_check_failed" in res.error
    assert sent == []


def test_the_helper_turns_the_policy_verdict_into_a_block_flag(monkeypatch):
    _policy(monkeypatch, (False, "not_in_allowlist"))
    assert ports.policy_blocked("wechat", "10000000002@chatroom") == (True, "not_in_allowlist")
    _policy(monkeypatch, (True, "in_allowlist"))
    assert ports.policy_blocked("wechat", "10000000004@chatroom") == (False, "in_allowlist")
    _policy(monkeypatch, KeyError("boom"))
    assert ports.policy_blocked("wechat", "x") == (True, "policy_check_failed")


def test_a_dry_run_is_blocked_as_well(monkeypatch):
    sent = _recorder(monkeypatch)
    _policy(monkeypatch, (False, "not_in_allowlist"))
    res = asyncio.run(ports.say_many(["排练用的"], dry_run=True, chat_id="10000000002@chatroom"))
    assert res.ok is False and sent == []


def test_a_blocked_line_is_written_down_for_the_panel(monkeypatch, tmp_path):
    """面板要能说清「她被自己的名单挡住了」，所以被拦下的句子单独记一行（不进 outbound_queue）。"""
    import json

    sent = _recorder(monkeypatch)
    _policy(monkeypatch, (False, "not_in_allowlist"))
    log = tmp_path / "blocked.jsonl"
    monkeypatch.setattr(ports, "BLOCKED_LOG", str(log))

    res = asyncio.run(ports.say_many(["@风语 查宿舍呀"], dry_run=False,
                                     chat_id="10000000002@chatroom"))
    assert res.ok is False and sent == []
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(rows) == 1
    assert rows[0]["chat_id"] == "10000000002@chatroom"
    assert rows[0]["why"] == "not_in_allowlist"
    assert rows[0]["text"].startswith("@风语")
    assert rows[0]["at"]


def test_the_blocked_log_never_breaks_the_block(monkeypatch, tmp_path):
    """记不下也不能改判：被拦就是被拦。"""
    _recorder(monkeypatch)
    _policy(monkeypatch, (False, "not_in_allowlist"))
    monkeypatch.setattr(ports, "BLOCKED_LOG", str(tmp_path))   # 指向目录 -> 写必然失败

    res = asyncio.run(ports.say("你好", dry_run=False, chat_id="10000000002@chatroom"))
    assert res.ok is False and res.data.get("code") == "not_in_allowlist"


def test_the_panel_endpoint_reads_the_last_lines(monkeypatch, tmp_path):
    import json

    from console import executor

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "blocked.jsonl").write_text(
        "\n".join([json.dumps({"at": "2026-11-05 19:10:00", "chat_id": "c1",
                                "why": "not_in_allowlist", "text": "第一句"}, ensure_ascii=False),
                    "不是 JSON 的一行",
                    json.dumps({"at": "2026-11-05 19:11:00", "chat_id": "c2",
                                "why": "not_in_allowlist", "text": "第二句"}, ensure_ascii=False)]),
        encoding="utf-8")
    monkeypatch.setattr(executor, "ROOT", str(tmp_path))

    got = executor.speak_policy_blocked(limit=1)
    assert got["ok"] is True
    assert [x["chat_id"] for x in got["items"]] == ["c2"]
    assert got["bad_lines"] == 1

    same = executor.apply({"cmd": "speak_policy_blocked", "args": {"limit": 5}, "dry_run": True})
    assert same["ok"] is True and len(same["items"]) == 2


def test_the_panel_endpoint_says_so_when_nothing_was_blocked(monkeypatch, tmp_path):
    from console import executor

    monkeypatch.setattr(executor, "ROOT", str(tmp_path))
    got = executor.speak_policy_blocked(limit=3)
    assert got["ok"] is True and got["items"] == []
    assert "还没有话被挡下来" in str(got.get("note") or "")
