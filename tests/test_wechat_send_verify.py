"""微信发送后回读对账（``core/execution/gui/wechat_send.py``）的回归。

跑法：``python -X utf8 -u tests\test_wechat_send_verify.py``
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features  # noqa: E402
from core.execution.gui import wechat, wechat_send as ws  # noqa: E402
from core.execution.gui.base import GuiError  # noqa: E402

FAILED = 0


def check(name: str, cond: object, detail: str = "") -> None:
    global FAILED
    if cond:
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name}  {detail}")


SEEN: dict[str, object] = {}


class FakeDeps:
    """只记「按了几次回车 / @ 了谁」。填字那一段由 wechat.fill_message 的假替身负责。"""

    def __init__(self, press: bool = True, still_typed=None, mention_ok: bool = True) -> None:
        self.mentions: list[str] = []
        self._mention_ok = mention_ok
        self.presses = 0
        self._press = press
        self._still_typed = still_typed

    def mention(self, name: str) -> bool:
        self.mentions.append(name)
        return self._mention_ok

    def press_enter(self) -> bool:
        self.presses += 1
        return self._press

    def message_box_has_text(self):
        return self._still_typed

class ExplodingDeps:
    def press_enter(self) -> bool:  # pragma: no cover - 被调用就说明闸没关
        raise AssertionError("不该按回车")


class FakeStore:
    def __init__(self, boom: bool = False) -> None:
        self.rows: list[dict] = []
        self.boom = boom

    def record_receipt(self, receipt, *, turn_id: str, chat_id: str = "") -> bool:
        if self.boom:
            raise RuntimeError("回执库写挂了")
        self.rows.append({"turn_id": turn_id, "chat_id": chat_id, "receipt": dict(receipt)})
        return True


FILL_CALLS: list[tuple[str, str]] = []
ROWS_CALLS: list[dict] = []


@contextmanager
def scenario(*, mode="on", baseline=0, fill_error=None, press=True, rows=None,
             rows_error=False, filled_verified=True, still_typed=None, mention_ok=True):
    """把三个外部依赖换成假的：开关、库、界面。"""
    saved = {
        "mode": ws._mode,
        "fill": wechat.fill_message,
        "max": wechat.max_local_id,
        "rows": wechat.rows_after,
        "db": wechat._decrypted_db,
        "name": wechat.display_name_for,
    }
    FILL_CALLS.clear()
    ROWS_CALLS.clear()
    deps = FakeDeps(press, still_typed, mention_ok) if press is not None else ExplodingDeps()

    def fake_mode() -> str:
        return mode

    def fake_fill(chat, text, *, mention="", deps=None):
        SEEN["mention"] = mention
        FILL_CALLS.append((chat, text))
        if fill_error is not None:
            raise fill_error
        return type("Fill", (), {
            "text_sha256": ws.text_fingerprint(text),
            "filled_verified": filled_verified,
        })()

    def fake_max(path, chat, **kw):
        return baseline

    def fake_rows(path, chat, *, after_local_id=0, limit=20, **kw):
        ROWS_CALLS.append({"chat": chat, "after": after_local_id, "limit": limit})
        if rows_error:
            raise GuiError("not_connected", "库挂了")
        return list(rows or [])

    ws._mode = fake_mode
    wechat.fill_message = fake_fill
    wechat.max_local_id = fake_max
    wechat.rows_after = fake_rows
    wechat._decrypted_db = lambda account_id="": Path("fake.db")
    # 地址 → 名字：生产里查 contact.db，这里只钉「地址必须被翻成名字再进搜索框」。
    wechat.display_name_for = lambda chat: (
        "Elaina" if str(chat).startswith("wxid_") else str(chat)
    )
    try:
        yield deps
    finally:
        ws._mode = saved["mode"]
        wechat.fill_message = saved["fill"]
        wechat.max_local_id = saved["max"]
        wechat.rows_after = saved["rows"]
        wechat._decrypted_db = saved["db"]
        wechat.display_name_for = saved["name"]


def row(text: str, *, local_id=101, server_id=9001, mine=True) -> dict:
    return {
        "local_id": local_id,
        "server_id": server_id,
        "text": text,
        "guessed_sender": "",
        "mine": mine,
        "kind": 1,
        "time": 1781790000,
    }


def main() -> int:
    src = (ROOT / "core/execution/gui/wechat_send.py").read_text(encoding="utf-8")
    wx_src = (ROOT / "core/execution/gui/wechat.py").read_text(encoding="utf-8")

    # ---- 开关 ----
    check("开关：WECHAT_SEND 在注册表", features.WECHAT_SEND in features.FLAG_REGISTRY)
    check(
        "开关：默认 off",
        features.mode(features.WECHAT_SEND, features.MODE_OFF) == features.MODE_OFF,
    )
    check("开关：名字就是 wechat_send", features.WECHAT_SEND == "wechat_send")

    # ---- 闸：off / shadow 都不许碰界面 ----
    with scenario(mode=features.MODE_OFF, press=None) as deps:
        r = ws.send_and_verify("群A", "在吗")
    check("off：blocked", r.status == ws.STATUS_BLOCKED, r.status)
    check("off：填字零次", FILL_CALLS == [], FILL_CALLS)
    check("off：不按回车", isinstance(deps, ExplodingDeps), type(deps).__name__)
    check("off：detail 指出闸", "WECHAT_SEND=off" in r.detail, r.detail)

    with scenario(mode=features.MODE_SHADOW, press=None):
        r = ws.send_and_verify("群A", "在吗")
    check("shadow：blocked", r.status == ws.STATUS_BLOCKED, r.status)
    check("shadow：连填字都不做", FILL_CALLS == [], FILL_CALLS)
    check("shadow：detail 说明不可逆", "影子档" in r.detail, r.detail)

    # ---- 参数闸 ----
    with scenario():
        r = ws.send_and_verify("  ", "在吗")
    check("on：没给会话 → no_target", r.status == ws.STATUS_BLOCKED and "no_target" in r.detail, r.detail)
    with scenario():
        r = ws.send_and_verify("群A", "   ")
    check("on：空文本 → unsupported", r.status == ws.STATUS_BLOCKED and "unsupported" in r.detail, r.detail)

    # ---- 地址翻名字（2026-10-21 真机：搜索框里被打进 wxid） ----
    with scenario():
        ws.send_and_verify("wxid_seed01", "在吗")
    check("地址被翻成名字再进搜索框", FILL_CALLS and FILL_CALLS[0][0] == "Elaina", FILL_CALLS)
    check("回执里仍记地址（地址才是投递对象）",
          ws.send_and_verify("wxid_seed01", "在吗").chat == "wxid_seed01")
    check("wechat 有 display_name_for",
          "def display_name_for(chat: str) -> str:" in wx_src)
    check("send_and_verify 真的用了它", "shown = wechat.display_name_for(chat)" in src)
    check("填字用的是名字不是地址", "wechat.fill_message(shown," in src)

    # ---- 基线读不出来 ----
    saved_max = wechat.max_local_id
    wechat.max_local_id = lambda *a, **kw: (_ for _ in ()).throw(GuiError("not_connected", "库不在"))
    with scenario(mode="on"):
        wechat.max_local_id = lambda *a, **kw: (_ for _ in ()).throw(GuiError("not_connected", "库不在"))
        r = ws.send_and_verify("群A", "在吗")
    wechat.max_local_id = saved_max
    check("基线失败 → blocked", r.status == ws.STATUS_BLOCKED, r.status)
    check("基线失败：detail 带错误码", r.detail.startswith("not_connected"), r.detail)
    check("基线失败：没有填字", FILL_CALLS == [], FILL_CALLS)

    # ---- 填字失败 ----
    with scenario(fill_error=GuiError("no_target", "输入框没聚焦")) as deps:
        r = ws.send_and_verify("群A", "在吗")
    check("填字失败 → failed", r.status == ws.STATUS_FAILED, r.status)
    check("填字失败：detail 前缀", r.detail.startswith("fill_failed"), r.detail)
    check("填字失败：不按回车", deps.presses == 0, deps.presses)

    # ---- 回车没按下去 ----
    with scenario(press=False) as deps:
        r = ws.send_and_verify("群A", "在吗", deps=deps)
    check("回车失败 → failed", r.status == ws.STATUS_FAILED, r.status)
    check("回车失败：detail 前缀", r.detail.startswith("press_failed"), r.detail)
    check("回车失败：不去回读", ROWS_CALLS == [], ROWS_CALLS)
    check("回车只尝试一次", deps.presses == 1, deps.presses)

    # ---- 成功：新增行 + server_id ----
    store = FakeStore()
    with scenario(baseline=31, rows=[row("在吗")]):
        r = ws.send_and_verify("群A", "在吗", store=store, turn_id="turn-1", sleep=lambda s: None)
    check("成功：sent", r.status == ws.STATUS_SENT, r.status)
    check("成功：local_id/server_id", (r.local_id, r.server_id) == (101, 9001), (r.local_id, r.server_id))
    check("成功：只回读一次", r.polls == 1, r.polls)
    check("成功：只认新增行", ROWS_CALLS and ROWS_CALLS[0]["after"] == 31, ROWS_CALLS)
    rec = r.as_receipt()
    check("回执：ack=platform_id", rec["ack"] == "platform_id", rec["ack"])
    check("回执：client_msg_id", rec["client_msg_id"] == "local:101", rec["client_msg_id"])
    check("回执：ref", rec["ref"] == "wechat#101", rec["ref"])
    check("回执：adapter=wechat", rec["adapter"] == "wechat", rec["adapter"])
    check("回执：落库一次", len(store.rows) == 1 and store.rows[0]["turn_id"] == "turn-1", store.rows)

    # ---- 落了库但服务器号还没回来 ----
    with scenario(baseline=31, rows=[row("在吗", server_id=0)]):
        r = ws.send_and_verify("群A", "在吗", sleep=lambda s: None)
    check("无 server_id → unconfirmed", r.status == ws.STATUS_UNCONFIRMED, r.status)
    check("unconfirmed 也算发出去了", r.is_delivered() is True)
    check("unconfirmed：ack", r.as_receipt()["ack"] == "unconfirmed", r.as_receipt()["ack"])

    # ---- 内容对不上 / 不是我发的 ----
    waits: list[float] = []
    with scenario(baseline=31, rows=[row("别的话")], still_typed=True) as deps_wrong_text:
        r = ws.send_and_verify("群A", "在吗", polls=3, sleep=waits.append,
                               deps=deps_wrong_text)
    check("内容对不上 → failed", r.status == ws.STATUS_FAILED, r.status)
    check("内容对不上：detail 说明不重发", "不重发" in r.detail, r.detail)
    check("轮询次数用满", r.polls == 3, r.polls)
    check("等待 polls-1 次", len(waits) == 2, len(waits))

    with scenario(baseline=31, rows=[row("在吗", mine=False)], still_typed=True) as deps_not_mine:
        r = ws.send_and_verify("群A", "在吗", polls=2, sleep=lambda s: None,
                               deps=deps_not_mine)
    check("不是我发的 → 不算", r.status == ws.STATUS_FAILED, r.status)

    # ---- 回读时库抛错：不许把回合打挂 ----
    with scenario(baseline=31, rows_error=True, still_typed=True) as deps_rows_error:
        r = ws.send_and_verify("群A", "在吗", polls=2, sleep=lambda s: None,
                               deps=deps_rows_error)
    check("回读抛错 → failed 而不是崩", r.status == ws.STATUS_FAILED, r.status)

    # ---- 回执库写挂了也不影响回执 ----
    with scenario(baseline=31, rows=[row("在吗")]):
        r = ws.send_and_verify("群A", "在吗", store=FakeStore(boom=True), sleep=lambda s: None)
    check("回执落库失败：回执照返回", r.status == ws.STATUS_SENT, r.status)

    # ---- blocked 的回执形状 ----
    blocked = ws.SendReceipt(status=ws.STATUS_BLOCKED, chat="群A")
    check("blocked：ack=none", blocked.as_receipt()["ack"] == "none", blocked.as_receipt())
    check("blocked：reason 保留状态", blocked.as_receipt()["reason"] == "blocked")

    # ---- 指纹 ----
    check("指纹：抹空白", ws.text_fingerprint(" 你好  世界 ") == ws.text_fingerprint("你好 世界"))
    check("指纹：不同内容不同", ws.text_fingerprint("在吗") != ws.text_fingerprint("在吗？"))

    # ---- 库里没刷出来，但输入框已空：报「已发未确认」，不报失败 ----
    with scenario(baseline=31, rows=[], still_typed=False) as deps_empty:
        r = ws.send_and_verify("群A", "在吗", polls=2, sleep=lambda s: None,
                               deps=deps_empty)
    check("输入框已空 + 库里没有：sent_unconfirmed", r.status == ws.STATUS_UNCONFIRMED, r.status)
    check("输入框已空 + 库里没有：ack=unconfirmed", r.as_receipt()["ack"] == "unconfirmed",
          r.as_receipt())
    check("输入框已空 + 库里没有：只按了一次回车", deps_empty.presses == 1, deps_empty.presses)
    check("输入框已空 + 库里没有：detail 说明刷盘延迟",
          "sent_unconfirmed" in r.detail, r.detail)

    with scenario(baseline=31, rows=[], still_typed=True) as deps_typed:
        r = ws.send_and_verify("群A", "在吗", polls=2, sleep=lambda s: None,
                               deps=deps_typed)
    check("输入框里字还在：failed（没发出去）", r.status == ws.STATUS_FAILED, r.status)
    check("输入框里字还在：detail 说还停着字", "还停着字" in r.detail, r.detail)

    with scenario(baseline=31, rows=[], still_typed=None) as deps_none:
        r = ws.send_and_verify("群A", "在吗", polls=1, sleep=lambda s: None,
                               deps=deps_none)
    check("探针拿不准：按 failed 交给人判", r.status == ws.STATUS_FAILED, r.status)

    # ---- 等待窗口要覆盖微信落盘延迟（实测 26 秒）----
    check("默认轮询间隔 1.5s", ws.POLL_INTERVAL_S == 1.5, ws.POLL_INTERVAL_S)
    check("默认等待 >= 20 秒", ws.POLL_INTERVAL_S * ws.POLLS >= 20,
          ws.POLL_INTERVAL_S * ws.POLLS)

    # ---- 群里 @ 一个人：mention 必须透传给 fill_message（@ 在正文之前）----
    SEEN.clear()
    with scenario(baseline=31, rows=[row("@小奇 在吗")]) as deps_mention:
        r = ws.send_and_verify("鲸鲸的测试群", "@小奇 在吗", mention="小奇", polls=1,
                               sleep=lambda s: None, deps=deps_mention)
    check("@ 透传给填字那一层", SEEN.get("mention") == "小奇", SEEN.get("mention"))
    check("@ 后照常发出去", r.status == ws.STATUS_SENT, r.status)

    # @ 失败在 fill_message 里变成 GuiError，所以这里从上游就该是 failed 且不按回车
    with scenario(baseline=31, fill_error=GuiError("unsupported", "mention_failed：没 @ 上 小奇")) as deps_mention_fail:
        r = ws.send_and_verify("鲸鲸的测试群", "在吗", mention="小奇", polls=1,
                               sleep=lambda s: None, deps=deps_mention_fail)
    check("@ 失败 → failed", r.status == ws.STATUS_FAILED, r.status)
    check("@ 失败 → 不按回车", deps_mention_fail.presses == 0, deps_mention_fail.presses)
    check("@ 失败 → detail 说 mention", "mention" in r.detail.lower(), r.detail)

    # ---- 输入框读不到我们写的字：一次回车都不许按 ----
    with scenario(baseline=31, rows=[row("在吗")], filled_verified=False) as deps_unverified:
        r = ws.send_and_verify("群A", "在吗", polls=2, sleep=lambda s: None)
    check("填字没验证上：failed", r.status == ws.STATUS_FAILED, r.status)
    check("填字没验证上：一次回车都不按", deps_unverified.presses == 0, deps_unverified.presses)
    check("填字没验证上：不去回读", ROWS_CALLS == [], ROWS_CALLS)
    check("填字没验证上：detail 说明原因", "fill_unverified" in r.detail, r.detail)

    # ---- 回读前刷新解密库（读旧快照永远读不到新行）----
    refreshes: list[int] = []
    with scenario(baseline=31, rows=[row("在吗")]):
        r = ws.send_and_verify(
            "群A", "在吗", polls=1, sleep=lambda s: None,
            refresh=lambda: refreshes.append(1),
        )
    check("刷新：开跑前一次 + 每轮回读前一次", len(refreshes) == 2, refreshes)
    check("刷新：刷新后照样 sent", r.status == ws.STATUS_SENT, r.status)
    with scenario(baseline=31, rows=[row("在吗")]):
        r = ws.send_and_verify(
            "群A", "在吗", polls=1, sleep=lambda s: None,
            refresh=lambda: (_ for _ in ()).throw(RuntimeError("刷新挂了")),
        )
    check("刷新抛错：不影响判定（仍 sent）", r.status == ws.STATUS_SENT, r.status)

    # ---- 切不到会话就不许填字（open_chat 的失败原来被丢了）----
    class RefusingDeps:
        def window_title(self):
            return "微信"

        def foreground_title(self):
            return "微信"

        def open_chat(self, chat):
            return {"ok": False, "error": "搜索验证失败: 未找到姓名的可靠匹配"}

        def focus_message_box(self):
            raise AssertionError("切不到会话还去点输入框")

    saved_mode = wechat._mode
    wechat._mode = lambda: "on"
    try:
        try:
            wechat.fill_message("群A", "在吗", deps=RefusingDeps())
            refused, why = False, "没抛异常"
        except GuiError as exc:
            refused, why = exc.code == "no_target", f"{exc.code}:{exc.detail}"
    finally:
        wechat._mode = saved_mode
    check("切不到会话：抛 no_target 而不是继续填", refused, why)

    # ---- 静态纪律 ----
    check("只发一次：fill_message 调用点唯一", src.count("wechat.fill_message(") == 1, src.count("wechat.fill_message("))
    check("只发一次：press_enter 调用点唯一", src.count("impl.press_enter()") == 1, src.count("impl.press_enter()"))
    check("没有重试语义", "retry" not in src.lower() and "for _ in range(2" not in src)
    check("wechat.py 不按回车（没有 VK_RETURN）", "VK_RETURN" not in wx_src)
    check("wechat.py 提供 press_enter 协议位", "def press_enter(self) -> bool: ..." in wx_src)
    check("wechat.py 核对 open_chat 的 ok", 'opened.get("ok") is False' in wx_src)
    check("wechat.py 提供 refresh_db", "def refresh_db(" in wx_src)
    check("发送路径在回读前刷新", "    _refresh()\n    try:\n        baseline" in src)

    print(f"RESULT {FAILED} failed")
    return FAILED


if __name__ == "__main__":
    raise SystemExit(main())
