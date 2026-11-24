"""窗口唤出：托盘里/最小化时先把微信叫到前台，叫不出来就不许往下走。

跑法：``python -X utf8 -u tests/test_wechat_wake.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features  # noqa: E402
from core.execution.gui import wechat  # noqa: E402
from core.execution.gui.base import GuiError  # noqa: E402

_FAILED: list[str] = []
_TOTAL = 0


def check(name: str, cond: object, detail: str = "") -> None:
    global _TOTAL
    _TOTAL += 1
    if cond:
        print(f"  ok   {name}")
        return
    _FAILED.append(name)
    print(f"  FAIL {name}  {detail}")


class FakeDeps:
    """够 fill_message 跑到「唤窗口」这一步的最小壳。"""

    def __init__(self, *, foreground: str = "Google Chrome", woke: bool = True, title: str = "微信"):
        self._foreground = foreground
        self._woke = woke
        self._title = title
        self.calls: list[str] = []

    def window_title(self) -> str:
        return self._title

    def foreground_title(self) -> str:
        return self._foreground

    def wake(self) -> bool:
        self.calls.append("wake")
        if self._woke:
            self._foreground = self._title
        return self._woke

    def open_chat(self, chat: str):
        self.calls.append(f"open_chat:{chat}")
        return {"ok": True, "who": chat}

    def focus_message_box(self) -> bool:
        self.calls.append("focus")
        return False  # 到这一步就够证明了：唤窗发生在更早

    def paste(self, text: str) -> bool:
        self.calls.append("paste")
        return True

    def press_enter(self) -> bool:
        self.calls.append("press_enter")
        return False

    def message_box_has_text(self):
        return True

    def mention(self, name: str) -> bool:
        self.calls.append(f"mention:{name}")
        return True

    def screenshot(self):
        return None


def with_mode(value: str):
    original = wechat._mode
    wechat._mode = lambda: value
    return original


def reuse_gate() -> None:
    """丙：`_already_there` 是复用判断的唯一入口；读不到顶栏当没到，并落一条事件。"""
    from core.execution.gui import wechat as gw

    asked: list[str] = []

    def _yes(chat: str) -> tuple[bool, str]:
        asked.append(chat)
        return (True, chat)

    def _no(chat: str) -> tuple[bool, str]:
        asked.append(chat)
        return (False, "")

    def _boom(chat: str) -> tuple[bool, str]:
        asked.append(chat)
        raise RuntimeError("顶栏读不到")

    print("[6] 复用判断住在适配层")
    check("顶栏对得上就用现成的", gw._already_there("文件传输助手", verify=_yes) is True)
    check("顶栏对不上就老老实实搜索", gw._already_there("文件传输助手", verify=_no) is False)
    check("读失败当没到，不许抛出去", gw._already_there("文件传输助手", verify=_boom) is False)
    check("三次都真的问过顶栏", len(asked) == 3, repr(asked))


def main() -> int:
    print("[1] 唤不出来就不往下走")
    original = with_mode(features.MODE_ON)
    try:
        deps = FakeDeps(woke=False)
        try:
            wechat.fill_message("文件传输助手", "在吗", deps=deps)
            check("必须抛 GuiError", False, "没抛")
        except GuiError as exc:
            check("错误码是 no_target", exc.code == "no_target", exc.code)
            check("原因写清「唤不出来」", "唤不出来" in str(exc), str(exc))
        check("唤过一次", deps.calls.count("wake") == 1, repr(deps.calls))
        check("没去切会话", not any(c.startswith("open_chat") for c in deps.calls), repr(deps.calls))
        check("没往输入框打一个字", "paste" not in deps.calls, repr(deps.calls))

        print("[2] 唤出来了就继续")
        deps2 = FakeDeps(woke=True)
        try:
            wechat.fill_message("文件传输助手", "在吗", deps=deps2)
        except GuiError as exc:
            check("随后卡在输入框（这是预期）", exc.code == "no_target", exc.code)
        check("先唤醒再切会话", deps2.calls[:2] == ["wake", "open_chat:文件传输助手"], repr(deps2.calls))

        print("[3] 已经在前台就不去唤")
        deps3 = FakeDeps(foreground="微信")
        try:
            wechat.fill_message("文件传输助手", "在吗", deps=deps3)
        except GuiError:
            pass
        check("没调 wake", "wake" not in deps3.calls, repr(deps3.calls))

        print("[4] 没有窗口标题的壳也不许炸")
        class NoWakeDeps(FakeDeps):
            wake = None  # type: ignore[assignment]

        deps4 = NoWakeDeps(woke=False)
        try:
            wechat.fill_message("文件传输助手", "在吗", deps=deps4)
            check("没有 wake 也 fail closed", False, "没抛")
        except (GuiError, TypeError) as exc:
            check("没有 wake 也 fail closed", True, type(exc).__name__)
    finally:
        wechat._mode = original

    print("[5] 真函数与静态接线")
    check("wake_window 存在", callable(getattr(wechat, "wake_window", None)))
    src = (ROOT / "core/execution/gui/wechat.py").read_text(encoding="utf-8")
    check("协议位有 wake", "def wake(self) -> bool:" in src)
    check("_Deps 的 wake 走 wake_window", "return wake_window()" in src)
    check("fill_message 里先唤再切", src.index('wake = getattr(impl, "wake", None)') < src.index("opened = impl.open_chat(chat)"), "顺序反了")
    check("三段式：UIA + activate_window + 验一次",
          all(k in src for k in ("AutomationBackend().show(", "wx_vision.activate_window(", "IsIconic(")),
          "少了某一段")

    reuse_gate()

    print(f"RESULT {len(_FAILED)} failed")
    for name in _FAILED:
        print(f"  - {name}")
    return len(_FAILED)


if __name__ == "__main__":
    raise SystemExit(main())
