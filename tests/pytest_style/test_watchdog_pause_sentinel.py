"""暂停哨兵要有时效：别让一个忘了删的文件把她静音一整天。

2026-11-04（E-55）：`logs/watchdog.stop` 从上午留到下午，看门狗每 5 分钟来一趟、
写一句"主人已暂停"、走了 —— 引擎躺了五个半小时，`messages` 里 11:52–17:20 入站与
自发同时为 0，而日志读起来像是主人故意的。这里钉住两件事：

1. 哨兵超过 `PAUSE_MAX_HOURS` 就当"忘了删"，照常拉起，并留一条 `sentinel_expired`；
2. 她停机超过 `DOWN_ALERT_HOURS` 就单独喊一声 `down_alert`（每小时最多一次），
   因为在"哨兵在"这条路上一行告警都不会写。
"""
from __future__ import annotations

import pytest
import importlib.util
import json
import os
import pathlib
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load():
    path = _ROOT / "scripts" / "watchdog_yinyue.py"
    spec = importlib.util.spec_from_file_location("watchdog_yinyue_pause", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wd = _load()


def _age_file(path, hours: float) -> None:
    stamp = time.time() - hours * 3600.0
    os.utime(path, (stamp, stamp))


def _actions(alert_file) -> list[str]:
    try:
        lines = alert_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        if not line.strip():
            continue
        out.append(str(json.loads(line).get("action") or ""))
    return out


def _run_main(tmp_path, *, stop_hours: float, log_hours: float = 0.0):
    stop = tmp_path / "watchdog.stop"
    stop.write_text("x", encoding="utf-8")
    _age_file(stop, stop_hours)
    log = tmp_path / "svc.log"
    log.write_text("x", encoding="utf-8")
    _age_file(log, log_hours)
    alert = tmp_path / "wd.jsonl"
    rc = wd.main([
        "--status-url", "http://127.0.0.1:1/api/status", "--port", "1",
        "--attempts", "1", "--interval", "0.01", "--timeout", "0.05",
        "--alert-file", str(alert), "--log-file", str(log),
        "--lock-file", str(tmp_path / "wd.lock"), "--no-lock",
        "--stop-file", str(stop), "--dry-run",
    ])
    return rc, _actions(alert)


def test_a_fresh_sentinel_still_means_paused(tmp_path):
    stop = tmp_path / "watchdog.stop"
    stop.write_text("x", encoding="utf-8")
    expired, age = wd.pause_sentinel_expired(stop)
    assert expired is False
    assert 0.0 <= float(age) < 0.1


def test_a_sentinel_left_for_hours_is_treated_as_forgotten(tmp_path):
    stop = tmp_path / "watchdog.stop"
    stop.write_text("x", encoding="utf-8")
    now = time.time()
    _age_file(stop, 3.0)
    expired, age = wd.pause_sentinel_expired(stop, now=now)
    assert expired is True
    assert 2.9 < float(age) < 3.1


def test_no_sentinel_is_never_expired(tmp_path):
    assert wd.pause_sentinel_expired(tmp_path / "nope.stop") == (False, None)


def test_a_future_stamp_does_not_go_negative(tmp_path):
    stop = tmp_path / "watchdog.stop"
    stop.write_text("x", encoding="utf-8")
    now = time.time()
    stamp = now + 3600
    os.utime(stop, (stamp, stamp))
    expired, age = wd.pause_sentinel_expired(stop, now=now)
    assert (expired, age) == (False, 0.0)


def test_the_threshold_is_adjustable(tmp_path):
    stop = tmp_path / "watchdog.stop"
    stop.write_text("x", encoding="utf-8")
    now = time.time()
    _age_file(stop, 1.0)
    assert wd.pause_sentinel_expired(stop, now=now, max_hours=0.5)[0] is True
    assert wd.pause_sentinel_expired(stop, now=now, max_hours=24.0)[0] is False


def test_down_alert_needs_a_long_stop():
    assert wd.down_alert_due(0.99, None) is False
    assert wd.down_alert_due(1.0, None) is True


def test_down_alert_repeats_once_per_hour():
    assert wd.down_alert_due(3.0, 0.5) is False
    assert wd.down_alert_due(3.0, 1.0) is True
    assert wd.down_alert_due(3.0, 4.0) is True


def test_an_expired_sentinel_does_not_halt_the_watchdog(tmp_path):
    rc, acts = _run_main(tmp_path, stop_hours=3.0)
    assert "halted" not in acts
    assert "sentinel_expired" in acts
    assert rc == wd.EXIT_FAILED


def test_a_fresh_sentinel_still_halts(tmp_path):
    rc, acts = _run_main(tmp_path, stop_hours=0.0)
    assert acts.count("halted") == 1
    assert "sentinel_expired" not in acts
    assert rc == wd.EXIT_HALTED


def test_a_long_stop_is_reported_even_while_paused(tmp_path):
    rc, acts = _run_main(tmp_path, stop_hours=0.0, log_hours=2.0)
    assert "down_alert" in acts
    assert rc == wd.EXIT_HALTED


def test_the_last_down_alert_time_is_measured(tmp_path):
    from datetime import datetime

    now = 1793795000.0
    at = datetime.fromtimestamp(now - 7200).astimezone().isoformat(timespec="seconds")
    alert = tmp_path / "wd.jsonl"
    alert.write_text(json.dumps({"action": "down_alert", "at": at}) + "\n", encoding="utf-8")
    hours = wd._last_down_alert_hours(alert, now=now)
    assert 1.9 < float(hours) < 2.1


def test_a_corrupt_alert_line_is_reported_not_swallowed(tmp_path, capsys):
    alert = tmp_path / "wd.jsonl"
    alert.write_text('{"action": "halted"}\nnot json at all\n', encoding="utf-8")
    assert wd._last_down_alert_hours(alert, now=1793795000.0) is None
    err = capsys.readouterr().err
    assert "读不懂" in err
    assert "1 行" in err

# ---- 2026-11-05：她哑了多久（quiet_alert）----


def _root_with_self_msg(tmp_path, *, hours_ago, now, others_min_ago=1.0):
    import sqlite3 as _sq
    from datetime import datetime as _dt

    root = tmp_path / "repo"
    db = root / "data" / "platforms" / "wechat" / "wxid_x"
    db.mkdir(parents=True)
    c = _sq.connect(str(db / "account.db"))
    c.execute("create table messages (id text, chat_id text, content text,"
              " is_self integer, created_at text)")
    mine = _dt.fromtimestamp(now - hours_ago * 3600.0).strftime("%Y-%m-%d %H:%M:%S")
    theirs = _dt.fromtimestamp(now - others_min_ago * 60.0).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("insert into messages values ('1','c1','我自己说的',1,?)", (mine,))
    c.execute("insert into messages values ('2','c1','别人说的',0,?)", (theirs,))
    c.commit()
    c.close()
    return root


def test_quiet_alert_needs_six_hours_not_five(tmp_path):
    assert wd.quiet_alert_due(5.9, None) is False
    assert wd.quiet_alert_due(6.0, None) is True
    assert wd.quiet_alert_due(None, None) is False


def test_a_quiet_alert_is_not_repeated_every_run(tmp_path):
    # 喊过之后 5 分钟、50 分钟都不该再喊（否则就是 E-74 那种刷屏）
    assert wd.quiet_alert_due(6.1, 0.08) is False
    assert wd.quiet_alert_due(6.9, 0.9) is False
    assert wd.quiet_alert_due(7.0, 1.0) is True


def test_last_self_spoke_hours_ignores_what_others_said(tmp_path):
    now = 1793795000.0
    root = _root_with_self_msg(tmp_path, hours_ago=3.0, now=now)
    hours = wd.last_self_spoke_hours(root, now=now)
    assert 2.9 < float(hours) < 3.1


def test_no_account_db_means_unknown_not_silent(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    assert wd.last_self_spoke_hours(empty, now=1793795000.0) is None


def test_the_quiet_alert_is_written_once_per_hour(tmp_path):
    now = 1793795000.0
    root = _root_with_self_msg(tmp_path, hours_ago=7.0, now=now)
    alert = tmp_path / "wd.jsonl"
    got = wd._check_quiet(alert, root=root, now=now)
    assert got is not None and 6.9 < float(got) < 7.1
    assert _actions(alert) == ["quiet_alert"]
    wd._check_quiet(alert, root=root, now=now + 300.0)   # 5 分钟后又来一趟
    assert _actions(alert) == ["quiet_alert"]            # 不许再喊

@pytest.fixture(autouse=True)
def _owner_notes(monkeypatch):
    """哑太久会给主人发一条；这里只记不发 —— 否则测试会真往 world.db 塞一条消息。"""
    from loop import owner_copy as _oc

    seen = []

    def _fake(kind, *, target="", detail="", **kw):
        seen.append({"kind": kind, "target": target, "detail": detail})
        return {"sent": True, "why": "", "gid": "g-test"}

    monkeypatch.setattr(_oc, "note", _fake)
    return seen


def test_the_quiet_alert_also_tells_the_owner(tmp_path, _owner_notes):
    now = 1793795000.0
    root = _root_with_self_msg(tmp_path, hours_ago=7.0, now=now)
    alert = tmp_path / "wd.jsonl"
    assert wd._check_quiet(alert, root=root, now=now) is not None
    assert [x["kind"] for x in _owner_notes] == ["quiet_alert"]
    assert 6.9 < float(_owner_notes[0]["detail"]) < 7.1
    line = json.loads(alert.read_text(encoding="utf-8").splitlines()[-1])
    assert line["action"] == "quiet_alert"
    assert line["owner_copy"] == "sent"


def test_a_broken_owner_copy_never_blocks_the_alert(tmp_path, monkeypatch):
    from loop import owner_copy as _oc

    def _boom(*a, **kw):
        raise RuntimeError("queue is gone")

    monkeypatch.setattr(_oc, "note", _boom)
    now = 1793795000.0
    root = _root_with_self_msg(tmp_path, hours_ago=7.0, now=now)
    alert = tmp_path / "wd.jsonl"
    assert wd._check_quiet(alert, root=root, now=now) is not None
    line = json.loads(alert.read_text(encoding="utf-8").splitlines()[-1])
    assert line["action"] == "quiet_alert"
    assert line["owner_copy"] == "failed"


def test_the_owner_env_is_read_from_a_dotenv_file(tmp_path, monkeypatch):
    """2026-11-05：计划任务用 pythonw 直接跑本脚本，`.env` 从没被加载过，
    于是每条 quiet_alert 都记 owner_copy="no_owner"，主人一次也没收到。"""
    monkeypatch.delenv("WECHAT_OWNER_WXIDS", raising=False)
    monkeypatch.delenv("YINYUE_OWNER_WXIDS", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text(
        "# 注释\nWECHAT_OWNER_WXIDS=wxid_owner_a\nPLATFORMS_QQ_X=1\n",
        encoding="utf-8")
    assert wd._owner_env(root) == 1
    assert os.environ["WECHAT_OWNER_WXIDS"] == "wxid_owner_a"


def test_an_existing_owner_env_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("WECHAT_OWNER_WXIDS", "wxid_keep")
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text("WECHAT_OWNER_WXIDS=wxid_new\n", encoding="utf-8")
    assert wd._owner_env(root) == 0
    assert os.environ["WECHAT_OWNER_WXIDS"] == "wxid_keep"


def test_a_missing_dotenv_is_not_an_error(tmp_path):
    assert wd._owner_env(tmp_path) == 0


def test_notifying_the_owner_goes_through_the_queue(monkeypatch):
    from loop import owner_copy as _oc

    seen = {}

    def _note(kind, **kw):
        seen.update(kw)
        seen["kind"] = kind
        return {"sent": True, "why": ""}

    monkeypatch.setattr(_oc, "note", _note)
    monkeypatch.setattr(wd, "_owner_env", lambda root=None: 0)
    assert wd._notify_owner(6.5) == "sent"
    assert seen["kind"] == "quiet_alert"
    assert str(seen["detail"]).startswith("6.5")


def test_alert_text_does_not_count_as_her_speaking(tmp_path):
    """告警本身也是 is_self=1；报给主人的「几小时」要把自己那条排掉。

    2026-11-22 实测：11-21 22:07、11-22 04:10、10:16 三条告警原文一模一样，
    都说「6.0 小时」——因为每条告警落地就把计时清零了。
    """
    import sqlite3 as _sq
    from datetime import datetime as _dt

    now = 1793795000.0
    root = tmp_path / "repo"
    db = root / "data" / "platforms" / "wechat" / "wxid_x"
    db.mkdir(parents=True)
    c = _sq.connect(str(db / "account.db"))
    c.execute("create table messages (id text, chat_id text, content text,"
              " is_self integer, created_at text, msg_type_name text)")
    old = _dt.fromtimestamp(now - 12.0 * 3600.0).strftime("%Y-%m-%d %H:%M:%S")
    alert = _dt.fromtimestamp(now - 6.0 * 3600.0).strftime("%Y-%m-%d %H:%M:%S")
    call = _dt.fromtimestamp(now - 2.0 * 3600.0).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("insert into messages values ('1','c1','我自己说的',1,?,'text')", (old,))
    c.execute("insert into messages values ('2','c1','我已经 6.0 小时没有自己开口说过一句话了',1,?,'text')",
              (alert,))
    c.execute("insert into messages values ('3','c1','语音通话已经结束',1,?,'system')", (call,))
    c.commit()
    c.close()
    plain = wd.last_self_spoke_hours(root, now=now)
    honest = wd.last_self_spoke_hours(root, now=now, ignore_alert=True)
    # plain 把 system 行（2 小时前的「语音通话已经结束」）也算进去了
    assert 1.9 < float(plain) < 2.1
    # 排除告警原文和 system 行之后，才是她真正没有开口的时长
    assert 11.9 < float(honest) < 12.1

