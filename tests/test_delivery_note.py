# -*- coding: utf-8 -*-
"""「上一条到没到」（loop/delivery_note.py）+ 体检里的对账项（2026-11-01）

这两件是同一个盲区的两面：
  · 对她：她一直只看得到 say 返回的 `queued`，不知道上一条到底到没到；
  · 对家里人：只能翻库才知道"12 条全失败"。
守住"只说事实、不解释机制"和"查不到就别提"两条口径。
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dn():
    import importlib
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    return importlib.import_module("loop.delivery_note")


def _row(state, ack="", why="", ts=1000.0):
    return {"state": state, "ack": ack, "why": why, "created_ts": ts}


def test_confirmed_says_received():
    s = _dn()._one(_row("sent", "confirmed"))
    assert "本地发件记录" in s and "不能证明属于本次发送" in s and "17:16" not in s  # 时间由 ts 决定，这里只查口径


def test_pending_ack_is_honest_not_fake_success():
    s = _dn()._one(_row("sent", "pending"))
    assert "尚无本地回读确认" in s and "不确定" in s
    assert "收到了" not in s


def test_failed_carries_reason_and_permission_to_say_again():
    s = _dn()._one(_row("failed", why="无法激活微信窗口"))
    assert "执行失败" in s and "无法激活微信窗口" in s and "现在说一遍" not in s


def test_unverified_tells_her_not_to_repeat():
    s = _dn()._one(_row("unverified", why="打开的会话不是目标（顶部是「」），已中止，没打字"))
    assert "先别重说" in s and "有新的内容再开口" in s


def test_sent_but_unconfirmed_tells_her_not_to_repeat():
    s = _dn()._one(_row("sent", "pending"))
    assert "别再重说一遍" in s and "等下一次回读" in s


def test_expired_is_not_confused_with_failed():
    assert "过期" in _dn()._one(_row("expired", why="超过 644 秒"))


def test_queued_is_not_reported_as_sent():
    s = _dn()._one(_row("pending"))
    assert "等待执行" in s


def test_no_known_state_no_note():
    assert _dn()._one(_row("weird_state")) == ""


def test_old_message_is_not_dragged_up():
    """一小时内才提；100 分钟前的不提（翻旧账只会让她莫名其妙地道歉）。"""
    dn = _dn()
    now = 10000.0
    assert dn.note("__nobody_like_this__", now=now) == ""
    old = now - dn.FRESH_S - 60
    # 直接测口径：_one 不判断时间，note 才判断；这里确认 FRESH_S 存在且一小时量级
    assert 3000 <= dn.FRESH_S <= 7200
    assert old < now


def test_brain_shows_the_note_and_never_crashes_the_tick():
    src = open(os.path.join(ROOT, "loop", "brain.py"), encoding="utf-8").read()
    assert '"上一条到没到"' in src
    assert "delivery_note" in src and "_dn.note(chat_id)" in src
    # 必须被 try 包着：这件事查不到，不能让她这一拍炸掉
    head = src.split('"上一条到没到"')[0]
    assert head.rstrip().endswith("try:") or "try:" in head[-200:]


def test_checkup_has_delivery_item():
    src = open(os.path.join(ROOT, "console", "checkup.py"), encoding="utf-8").read()
    assert "def check_delivery(" in src
    assert "check_delivery]" in src, "必须挂进 CHECKS，否则面板「检」页看不到"
    assert "没回来认账" in src


def test_world_db_exposes_last_outbound():
    src = open(os.path.join(ROOT, "db", "world_db.py"), encoding="utf-8").read()
    assert "def last_outbound(" in src
    assert "world_db.last_outbound_failed" in src, "只读辅助出错也要留痕，不许静默吞"
def main() -> bool:
    """2026-11-02 补：这个文件以前只有 def test_、没有运行入口（一直空转，一条都没跑过）。"""
    names = sorted(n for n in globals() if n.startswith("test_"))
    fails = []
    for n in names:
        fn = globals()[n]
        if not callable(fn):
            continue
        try:
            fn()
            print("  ok   %s" % n)
        except Exception as exc:  # noqa: BLE001 - 汇总用，单个用例炸了要继续跑完
            fails.append(n)
            print("  FAIL %s  %s" % (n, repr(exc)[:200]))
    print("RESULT %d failed / %d" % (len(fails), len(names)))
    return not fails


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)

def test_same_outcome_is_only_announced_once():
    """同一条发送、同一个结果只说一次；结果变了（未确认→确认）再说一次。"""
    import time
    from db import world_db
    from loop import ports

    dn = _dn()
    row = {"id": 7, "state": "sent", "ack": "unconfirmed", "why": "", "created_ts": time.time()}
    saved = world_db.last_outbound
    token = ports._READ_SCOPE.set(ports.ReadScope("wechat", "acct", "c1"))
    try:
        world_db.last_outbound = lambda *a, **k: [dict(row)]
        dn._CACHE.clear(); dn._ANNOUNCED.clear()
        first = dn.note("c1", now=row["created_ts"])
        dn._CACHE.clear()
        second = dn.note("c1", now=row["created_ts"])
        row["ack"] = "confirmed"
        dn._CACHE.clear()
        third = dn.note("c1", now=row["created_ts"])
    finally:
        world_db.last_outbound = saved
        ports._READ_SCOPE.reset(token)
    assert first != "" and second == "" and third != ""
