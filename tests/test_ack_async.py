# -*- coding: utf-8 -*-
"""回执后台补账 + 出口作用域闸（2026-10-21）。

三件事各自的钉法：
- 回执**不在发送主路径上等**：`send_text` 立刻返回 `ack=pending`，后台线程补；
- 回读**只做增量**，绝不触发全量解密（那条会让每条回复慢几秒）；
- 出口作用域 `YINYUE_EXIT_SCOPE_CHATS`：名单外一律 `out_of_scope`，名单空 = 不设限。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402

from core import features  # noqa: E402
from core.execution import wx_sender  # noqa: E402
from core.speech import legacy_bridge  # noqa: E402
from core.terminal import delivery_receipts as receipts  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name}" + (f"  [{detail}]" if detail else ""))


def _src(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def _body(src: str, name: str) -> str:
    start = src.index(f"def {name}(")
    nxt = src.find("\ndef ", start + 1)
    return src[start:nxt if nxt > 0 else len(src)]


# ---------------------------------------------------------------- 作用域闸
def t_exit_scope() -> None:
    saved = os.environ.get(legacy_bridge.SCOPE_ENV)
    try:
        os.environ.pop(legacy_bridge.SCOPE_ENV, None)
        check("名单为空：不设限（向后兼容）", legacy_bridge.in_exit_scope("any") is True)

        os.environ[legacy_bridge.SCOPE_ENV] = "wxid_a, 10000000004@chatroom"
        check("名单解析会去空白", legacy_bridge.exit_scope() == ("wxid_a", "10000000004@chatroom"))
        check("名单内放行", legacy_bridge.in_exit_scope("wxid_a") is True)
        check("名单内放行（群）", legacy_bridge.in_exit_scope("10000000004@chatroom") is True)
        check("名单外拒绝", legacy_bridge.in_exit_scope("10000000003@chatroom") is False)
        check("空串一律拒绝（不猜）", legacy_bridge.in_exit_scope("") is False)
    finally:
        if saved is None:
            os.environ.pop(legacy_bridge.SCOPE_ENV, None)
        else:
            os.environ[legacy_bridge.SCOPE_ENV] = saved


def t_bridge_gate_order() -> None:
    src = _src("core/speech/legacy_bridge.py")
    body = _body(src, "deliver_via_exit")
    i_scope = body.find("in_exit_scope(")
    i_kind = body.find("KIND_MAP.get(")
    check("作用域闸在 kind 映射之前", 0 <= i_scope < i_kind, f"{i_scope} vs {i_kind}")
    check("越界状态词是 skipped/out_of_scope",
          'status="skipped", reason="out_of_scope"' in body)


# ---------------------------------------------------------------- 只做增量
def t_incremental_only() -> None:
    src = _src("core/execution/wx_sender.py")
    body = _body(src, "_lookup_ack")
    check("无绑定回读保留未知", wx_sender._lookup_ack("张三", "在吗", 7) is None)
    check("回读里没有裸 refresh_db()", "refresh_db()" not in body)
    check("适配器启用由人来给（默认关）",
          'ADAPTER_ENABLE_ENV = "YINYUE_WECHAT_ADAPTER_ENABLE"' in _src("core/execution/wechat_adapter.py"))
    check("全量兜底只在显式要求时才发生",
          "if full_fallback and fresh.exists()" in _src("core/execution/gui/wechat.py"))


def t_ack_polling(monkeypatch) -> None:
    hits = []

    def fake_lookup(who, msg, baseline):
        hits.append(baseline)
        return None if len(hits) < 3 else {"ack": "platform_id", "local_id": "9", "server_id": "S1"}

    import core.execution.gui.wechat as wxgui

    saved_lookup = wx_sender._lookup_ack
    saved_sleep = time.sleep
    wx_sender._lookup_ack = fake_lookup
    time.sleep = lambda s: None
    try:
        got = wx_sender.ack_after_send("张三", "在吗", 7, polls=3, interval_s=0.01)
    finally:
        wx_sender._lookup_ack = saved_lookup
        time.sleep = saved_sleep
    check("轮询到就返回平台号", got.get("ack") == "platform_id", str(got))
    check("轮询次数用满才拿到（第 3 次）", len(hits) == 3, str(hits))
    check("baseline 缺失 → 直接 unconfirmed（不查库）",
          wx_sender.ack_after_send("张三", "在吗", None).get("ack") == "unconfirmed")


def t_backfill_thread(monkeypatch) -> None:
    done = threading.Event()
    calls = []

    def fake_lookup(who, msg, baseline):
        return {"ack": "platform_id", "local_id": "31", "server_id": "4700371881238744822"}

    saved_lookup = wx_sender._lookup_ack
    saved_update = receipts.update_pending_ack
    wx_sender._lookup_ack = fake_lookup

    def fake_update(target, **kw):
        calls.append((target, kw))
        done.set()
        return 1

    receipts.update_pending_ack = fake_update
    try:
        check("baseline 缺失 → 不起线程", wx_sender.spawn_ack_backfill("张三", "在吗", None) is False)
        started = wx_sender.spawn_ack_backfill("张三", "在吗", 30)
        check("baseline 存在 → 起线程", started is True)
        check("线程真的补了账", done.wait(3.0) is True)
        check("补的是 platform_id 与消息号",
              calls and calls[0][1].get("ack") == "platform_id"
              and calls[0][1].get("platform_msg_id") == "4700371881238744822", str(calls[:1]))
    finally:
        wx_sender._lookup_ack = saved_lookup
        receipts.update_pending_ack = saved_update


def t_adapter_enable() -> None:
    """出口适配器的"准不准"由人给：YINYUE_WECHAT_ADAPTER_ENABLE（默认关）。"""
    from core.execution import wechat_adapter as wa

    saved = os.environ.get(wa.ADAPTER_ENABLE_ENV)
    try:
        os.environ.pop(wa.ADAPTER_ENABLE_ENV, None)
        wa.reset_default_registry()
        check("默认关：适配器登记但不启用",
              wa.default_registry().is_enabled("wechat") is False)

        os.environ[wa.ADAPTER_ENABLE_ENV] = "1"
        wa.reset_default_registry()
        check("=1：直接以启用态构建",
              wa.default_registry().is_enabled("wechat") is True)

        os.environ[wa.ADAPTER_ENABLE_ENV] = "yes"
        check("=yes 也算真", wa.adapter_enable_env() is True)
        os.environ[wa.ADAPTER_ENABLE_ENV] = "0"
        check("=0 算假", wa.adapter_enable_env() is False)
        os.environ[wa.ADAPTER_ENABLE_ENV] = " maybe "
        check("含糊值一律算假（不猜）", wa.adapter_enable_env() is False)
    finally:
        if saved is None:
            os.environ.pop(wa.ADAPTER_ENABLE_ENV, None)
        else:
            os.environ[wa.ADAPTER_ENABLE_ENV] = saved
        wa.reset_default_registry()


def t_send_does_not_wait() -> None:
    src = _src("core/execution/wx_sender.py")
    body = _body(src, "send_text")
    check("send_text 立刻回 pending", '_ACK_PENDING if baseline is not None' in body)
    check("send_text 不再同步调 ack_after_send", "ack_after_send(" not in body)
    check("send_text 起了后台补账", "spawn_ack_backfill(who, msg, baseline)" in body)


# ---------------------------------------------------------------- 补账 SQL
def _engine():
    eng = create_engine("sqlite://")
    for stmt in receipts.ddl_statements():
        with eng.begin() as conn:
            conn.exec_driver_sql(stmt)
    receipts.set_engine(eng)
    return eng


def t_update_pending_ack() -> None:
    eng = _engine()
    saved_mode = receipts._mode
    receipts._mode = lambda: features.MODE_ON
    try:
        # 用 record_receipt 落行（ts 由它写成 datetime.now()，与生产同一条路径）
        ok = receipts.record_receipt(
            {"status": "sent", "kind": "text", "target": "张三", "ack": "pending",
             "detail": "旧出口路径"},
            turn_id="t1", chat_id="张三",
        )
        check("pending 行先落进去了", ok is True)
        changed = receipts.update_pending_ack(
            "张三", ack="platform_id", platform_msg_id="S9", client_msg_id="L9"
        )
        check("无操作绑定不得补写", changed == 0, str(changed))
        with eng.begin() as conn:
            row = conn.exec_driver_sql(
                "SELECT ack, platform_msg_id, client_msg_id, status, detail FROM delivery_receipts"
            ).fetchone()
        check("ack 保持 pending", row[0] == "pending", str(row))
        check("消息号保持未知", row[1] == "" and row[2] == "", str(row))
        check("status 一个字不改（补账不许改判）", row[3] == "sent", str(row))
        check("detail 不伪造补账痕迹", row[4] == "旧出口路径", str(row))
        check("重复无绑定补账仍不动",
              receipts.update_pending_ack("张三", ack="platform_id") == 0)
        check("别的对象不碰", receipts.update_pending_ack("李四", ack="platform_id") == 0)
        receipts._mode = lambda: features.MODE_OFF
        check("off 档一行不改", receipts.update_pending_ack("张三", ack="platform_id") == 0)
    finally:
        receipts._mode = saved_mode
        receipts.set_engine(None)


def main() -> int:
    t_exit_scope()
    t_bridge_gate_order()
    t_incremental_only()
    t_ack_polling(None)
    t_backfill_thread(None)
    t_send_does_not_wait()
    t_adapter_enable()
    t_update_pending_ack()
    t_backfill_cancel_keeps_unconfirmed()
    print()
    print(f"RESULT {FAILED} failed / {PASSED} checks")
    return 0 if not FAILED else 1




def t_backfill_cancel_keeps_unconfirmed() -> None:
    """后台补账被取消/查询全失败时，只能保持 unconfirmed，不得补出平台号。"""
    calls = []
    saved_lookup = wx_sender._lookup_ack
    saved_update = receipts.update_pending_ack
    saved_polls = wx_sender.BACKFILL_POLLS
    saved_sleep = wx_sender.time.sleep
    saved_n = wx_sender._backfill_running["n"]

    def boom(who, msg, baseline):
        raise RuntimeError("cancelled")

    def capture(target, **kw):
        calls.append((target, kw))
        return 0

    wx_sender._lookup_ack = boom
    wx_sender.BACKFILL_POLLS = 2
    wx_sender.time.sleep = lambda _s: None
    receipts.update_pending_ack = capture
    try:
        wx_sender._backfill_running["n"] += 1
        got = wx_sender._backfill_ack("张三", "在吗", 30)
        check("取消后仍是 unconfirmed", got.get("ack") == "unconfirmed", str(got))
        check("取消后不补平台号", not got.get("server_id") and calls and calls[0][1].get("platform_msg_id") == "", str(calls[:1]))
        check("取消后线程计数归零", wx_sender._backfill_running["n"] == saved_n, str(wx_sender._backfill_running))
    finally:
        wx_sender._lookup_ack = saved_lookup
        wx_sender.BACKFILL_POLLS = saved_polls
        wx_sender.time.sleep = saved_sleep
        receipts.update_pending_ack = saved_update
        wx_sender._backfill_running["n"] = saved_n


if __name__ == "__main__":
    raise SystemExit(main())
