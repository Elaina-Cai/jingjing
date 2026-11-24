"""旧出口路径的回执（`docs/decisions/出口.md` §5.3）。

这套断言钉住四件事：
1. `SendResult` 带回执口径（平台消息号 / 未确认），不再把 `sent_at` 当消息号；
2. provider 把 `local_id` / `server_id` 透传上来；
3. `build_receipt` 只说真话：有号 = platform_id，没号 = unconfirmed；
4. 开关不是 on 就一行不写；写库抛错不许冒泡。
"""

from __future__ import annotations

import inspect
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features  # noqa: E402
from core.contracts.delivery import Receipt  # noqa: E402
from core.execution.provider import SendResult  # noqa: E402
from core.speech import receipts  # noqa: E402


class _Target:
    platform = "wechat"
    chat_id = "filehelper"


class _Applied:
    who = "文件传输助手"
    kind = "text"
    content = "鲸鲸自检"
    target = _Target()


class _Result:
    ok = True
    msg_id = "4324064563963797869"
    ack = "platform_id"
    local_id = "89"
    server_id = "4324064563963797869"


class _ResultNoId:
    ok = True
    msg_id = ""
    ack = "unconfirmed"
    local_id = ""
    server_id = ""


class FakeStore:
    def __init__(self, ok: bool = True, raises: bool = False) -> None:
        self.ok = ok
        self.raises = raises
        self.calls: list[dict] = []

    def record_receipt(self, receipt, *, turn_id, chat_id="", ts=None):
        if self.raises:
            raise RuntimeError("db down")
        self.calls.append({"receipt": receipt, "turn_id": turn_id, "chat_id": chat_id})
        return self.ok


def _source(path: str) -> str:
    return io.open(ROOT / path, encoding="utf-8").read()


def main() -> int:
    failed = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal failed
        if cond:
            print(f"PASS {name}")
            return
        failed += 1
        print(f"FAIL {name} :: {detail}")

    # 1) SendResult 的三个新字段
    base = SendResult(ok=True, msg_id="m")
    check("SendResult 默认 ack=platform_id", base.ack == "platform_id", base.ack)
    check("SendResult 默认 local_id 空", base.local_id == "", base.local_id)
    check("SendResult 默认 server_id 空", base.server_id == "", base.server_id)
    check("SendResult 字段可赋值", SendResult(ok=True, local_id="1", server_id="2",
                                               ack="unconfirmed").server_id == "2")

    # 2) provider 透传（静态：不许再把 sent_at 当消息号）
    psrc = _source("core/execution/provider.py")
    check("provider 不再把 sent_at 当 msg_id", 'msg_id=result.get("sent_at"' not in psrc, "still there")
    check("provider 透传 local_id", 'result.get("local_id"' in psrc, "missing")
    check("provider 透传 server_id", 'result.get("server_id"' in psrc, "missing")
    check("provider 判定 ack 兜底", '"unconfirmed"' in psrc, "missing")
    check("provider 失败也给 ack=error", 'ack="error"' in psrc, "missing")

    # 3) build_receipt：有号 = platform_id
    got = receipts.build_receipt(_Applied(), _Result())
    check("回执是 Receipt", isinstance(got, Receipt), type(got).__name__)
    check("status=sent", got.status == "sent", got.status)
    check("ack=platform_id", got.ack == "platform_id", got.ack)
    check("platform_msg_id=server_id", got.platform_msg_id == "4324064563963797869", got.platform_msg_id)
    check("client_msg_id=local_id", got.client_msg_id == "89", got.client_msg_id)
    check("adapter=平台", got.adapter == "wechat", got.adapter)
    check("target 用 chat_id", got.target == "filehelper", got.target)
    check("kind 透传", got.kind == "text", got.kind)
    check("detail 标明旧路径", "旧出口路径" in got.detail, got.detail)

    # 没有号 = unconfirmed（不许把弱证据写成强证据）
    weak = receipts.build_receipt(_Applied(), _ResultNoId())
    check("没号 ack=unconfirmed", weak.ack == "unconfirmed", weak.ack)
    check("没号 platform_msg_id 空", weak.platform_msg_id == "", weak.platform_msg_id)
    check("没号 client_msg_id 空", weak.client_msg_id == "", weak.client_msg_id)

    # 4) 开关：off 一行不写
    real_mode = receipts.receipt_mode
    try:
        receipts.receipt_mode = lambda: features.MODE_OFF
        store = FakeStore()
        check("off 档返 False", receipts.record_delivery_receipt(_Applied(), _Result(), store=store) is False)
        check("off 档不碰库", store.calls == [], str(len(store.calls)))

        receipts.receipt_mode = lambda: features.MODE_ON
        store = FakeStore()
        check("on 档返 True", receipts.record_delivery_receipt(_Applied(), _Result(), store=store) is True)
        check("on 档落一行", len(store.calls) == 1, str(len(store.calls)))
        call = store.calls[0]
        check("落库 receipt 正确", call["receipt"].platform_msg_id == "4324064563963797869",
              call["receipt"].platform_msg_id)
        check("turn_id 兜底 outbound:<会话 id>", call["turn_id"] == "outbound:filehelper", call["turn_id"])
        check("chat_id 落库", call["chat_id"] == "filehelper", call["chat_id"])

        store = FakeStore()
        receipts.record_delivery_receipt(_Applied(), _Result(), turn_id="turn-1", chat_id="chat-x", store=store)
        check("turn_id 显式优先", store.calls[0]["turn_id"] == "turn-1", store.calls[0]["turn_id"])
        check("chat_id 显式优先", store.calls[0]["chat_id"] == "chat-x", store.calls[0]["chat_id"])

        check("store 返 False 时也返 False",
              receipts.record_delivery_receipt(_Applied(), _Result(), store=FakeStore(ok=False)) is False)
        check("store 抛错不冒泡",
              receipts.record_delivery_receipt(_Applied(), _Result(), store=FakeStore(raises=True)) is False)

        class _Weird:
            who = ""
            kind = ""
            target = None

        check("形状不对不炸", receipts.record_delivery_receipt(_Weird(), object(), store=FakeStore()) is True)
    finally:
        receipts.receipt_mode = real_mode

    # 5) 接线：**文字路径**的成功分支必须调它、且只调一次。
    # 作用域锁到这一个方法，不锁整个文件：b50aba8 给贴纸三条路径也各补了一行
    # （文字有、贴纸没有 = 账本只记了一半），全局计数是那条修复的正当后果，
    # 这里要钉的是「文字这条路没有重复记账」。贴纸侧由
    # tests/test_sticker_receipt.py 单独钉（三条成功路径各一行、失败零行）。
    from core.speech.outbound import OutboundGateway  # noqa: E402

    osrc = _source("core/speech/outbound.py")
    text_src = inspect.getsource(OutboundGateway._send_text_or_file)
    check("outbound 定义了钩子", "def _record_delivery_receipt(" in osrc, "missing")
    # 自证：getsource 取到的必须是这一个方法。取到整个类/整个文件时，下面那个
    # count 会变成一个像代码问题的数字（_r13 那次就是 4），先在这里说人话。
    check("取到的是这一个方法、不是整个类",
          text_src.lstrip().startswith("async def _send_text_or_file("),
          repr(text_src.lstrip()[:60]))
    check("文字路径成功分支调用一次",
          text_src.count("self._record_delivery_receipt(") == 1
          and "skip=bridged is not None" in text_src,
          str(text_src.count("self._record_delivery_receipt(")))
    ok_at = text_src.index("if result.ok:")
    call_at = text_src.index("self._record_delivery_receipt(")
    ret_at = text_src.index("return OutboundResult(True, output=output, msg_id=result.msg_id", call_at)
    check("钩子在成功分支内、返回之前", ok_at < call_at < ret_at, f"{ok_at}/{call_at}/{ret_at}")
    check("贴纸侧另有专门用例", (ROOT / "tests" / "test_sticker_receipt.py").is_file(), "missing")
    check("receipts 不 import 出口自己",
          "import core.speech.rhythm" not in _source("core/speech/receipts.py"))

    # 6) wx_sender：基线在按回车之前，回读只在 on 档
    wsrc = _source("core/execution/wx_sender.py")
    check("wx_sender 有 ack_baseline", "def ack_baseline(" in wsrc, "missing")
    check("wx_sender 有 ack_after_send", "def ack_after_send(" in wsrc, "missing")
    body_start = wsrc.index("    def send_text(")
    body_end = wsrc.index("    @wx_chat.ui_locked", body_start)
    body = wsrc[body_start:body_end]
    check("基线在 press_enter 之前",
          body.index("baseline = ack_baseline(who)") < body.index("press_enter()"), "order")
    check("发送后起后台补账（不在主路径上等回执）",
          "spawn_ack_backfill(who, msg, baseline)" in body, "missing")
    check("send_text 立刻回 pending（微信落盘 26 秒，等不起）",
          "_ACK_PENDING if baseline is not None" in body, "missing")
    check("回读本身仍可轮询（ack_after_send 保留）", "def ack_after_send(" in wsrc, "missing")
    # 2026-11-17：WECHAT_SEND 闸门已迁到 GUI 发送适配器；wx_sender 的旧回读改成
    # 「没有账号/发送操作绑定就不猜游标」。按现役位置与现役纪律断言。
    send_src = _source("core/execution/gui/wechat_send.py")
    check("发送闸在 GUI 出口里", "features.mode(features.WECHAT_SEND" in send_src, "missing")
    check("旧回读不猜游标（无未绑定回执）",
          "def ack_baseline(who: str) -> int | None:" in wsrc and "def _lookup_ack(" in wsrc,
          "missing")

    print(f"RESULT {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
