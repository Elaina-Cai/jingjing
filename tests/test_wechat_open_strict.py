"""微信「切到哪个会话」的验收：顶栏说话，搜索列表不算数。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.execution import wx_chat as c  # noqa: E402
from core.execution import wx_human  # noqa: E402

FAILED = 0


def check(name: str, cond, detail: str = "") -> None:
    global FAILED
    if cond:
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name}  {detail}")


WIN = type("Win", (), {"hwnd": 424242})()


class World:
    """把 open_chat_strict 会碰到的外部世界全换成假的。"""

    def __init__(self, *, reuse=None, activated=True, received=True, landed=False,
                 fg=True, verify=(True, ""), window=True):
        self.reuse = reuse
        self.activated = activated
        self.received = received
        self.landed = landed
        self.fg = fg
        self.window = window
        self.entered = 0
        self.cleared = 0
        self.types = []
        self.verify_calls = []
        self._verify = verify if isinstance(verify, list) else [verify]
    def __enter__(self):
        self._saved = {
            "try_reuse_chat": c.try_reuse_chat,
            "_activate_wechat_search": c._activate_wechat_search,
            "type_text": c.type_text,
            "_wechat_window": c._wechat_window,
            "_search_field_received": c._search_field_received,
            "_query_landed_in_message_box": c._query_landed_in_message_box,
            "_clear_focused_field": c._clear_focused_field,
            "press_enter": c.press_enter,
            "ensure_foreground": c.ensure_foreground,
            "verify_active_chat_title": c.verify_active_chat_title,
            "foreground_hwnd": c.wx_vision.foreground_hwnd,
            "sleep_human": wx_human.sleep_human,
        }
        c.try_reuse_chat = lambda *a, **kw: self.reuse
        c._activate_wechat_search = lambda: (self.activated, (10, 20), (300, 40))
        c.type_text = lambda text: self.types.append(text)
        c._wechat_window = lambda **kw: WIN if self.window else None
        c._search_field_received = lambda *a, **kw: self.received
        c._query_landed_in_message_box = lambda *a, **kw: self.landed
        c._clear_focused_field = self._clear

        def _enter():
            self.entered += 1

        c.press_enter = _enter
        c.ensure_foreground = lambda: WIN.hwnd if self.fg else None
        c.wx_vision.foreground_hwnd = lambda: WIN.hwnd if self.fg else 0

        def _verify(display, raw_who="", *, win=None):
            self.verify_calls.append(display)
            idx = min(len(self.verify_calls) - 1, len(self._verify) - 1)
            return self._verify[idx]

        c.verify_active_chat_title = _verify
        wx_human.sleep_human = lambda *a, **kw: None
        return self

    def _clear(self):
        self.cleared += 1

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            if key == "foreground_hwnd":
                c.wx_vision.foreground_hwnd = value
            elif key == "sleep_human":
                wx_human.sleep_human = value
            else:
                setattr(c, key, value)
        return False
def main() -> int:
    src = (ROOT / "core/execution/wx_chat.py").read_text(encoding="utf-8")
    wx_src = (ROOT / "core/execution/gui/wechat.py").read_text(encoding="utf-8")

    with World() as w:
        got = c.open_chat_strict("文件传输助手")
    check("顶栏验收通过：ok", got.get("ok") is True, got)
    check("顶栏验收通过：source=header", got.get("source") == "header", got)
    check("顶栏验收通过：回车按了一次", w.entered == 1, w.entered)
    check("顶栏验收通过：只验一次", len(w.verify_calls) == 1, w.verify_calls)
    check("顶栏验收通过：搜的就是目标名", w.types == ["文件传输助手"], w.types)

    mismatch = "当前打开的聊天是「张三」，与目标「李四」不符，已中止发送"
    with World(verify=(False, mismatch)) as w:
        got = c.open_chat_strict("李四")
    check("顶栏不符：ok=False", got.get("ok") is False, got)
    check("顶栏不符：错误里点名实际会话", "张三" in (got.get("error") or ""), got)
    check("顶栏不符：不再重试", len(w.verify_calls) == 1, w.verify_calls)
    with World(verify=(False, "")) as w:
        got = c.open_chat_strict("李四", verify_tries=3)
    check("顶栏读不出：ok=False", got.get("ok") is False, got)
    check("顶栏读不出：试满 3 次", len(w.verify_calls) == 3, w.verify_calls)
    check("顶栏读不出：错误里说不猜", "不猜" in (got.get("error") or ""), got)

    with World(activated=False) as w:
        got = c.open_chat_strict("李四")
    check("搜索开不起来：ok=False", got.get("ok") is False, got)
    check("搜索开不起来：没输入名字", w.types == [], w.types)
    check("搜索开不起来：没按回车", w.entered == 0, w.entered)

    with World(received=False, landed=True) as w:
        got = c.open_chat_strict("李四")
    check("落进输入框：ok=False", got.get("ok") is False, got)
    check("落进输入框：清空了", w.cleared == 1, w.cleared)
    check("落进输入框：没按回车", w.entered == 0, w.entered)

    with World(fg=False) as w:
        got = c.open_chat_strict("李四")
    check("不在前台：ok=False", got.get("ok") is False, got)
    check("不在前台：不去验顶栏", w.verify_calls == [], w.verify_calls)

    with World(window=False) as w:
        got = c.open_chat_strict("李四")
    check("没窗口：ok=False", got.get("ok") is False, got)
    with World(reuse={"ok": True, "who": "李四", "verified": True, "source": "header"}) as w:
        got = c.open_chat_strict("李四")
    check("记忆命中：直接返回", got.get("source") == "header" and w.types == [], (got, w.types))
    check("记忆命中：不按回车", w.entered == 0, w.entered)

    with World() as w:
        got = c.open_chat_strict("   ")
    check("空名字：no_target", "no_target" in (got.get("error") or ""), got)
    check("空名字：什么都不做", w.types == [] and w.entered == 0, (w.types, w.entered))

    strict_body = src[src.index("def open_chat_strict("):src.index("def open_chat(who: str")]
    check("定义在 open_chat 之前", "def open_chat(" in src, "缺函数")
    check("顶栏验收用的是 verify_active_chat_title", "verify_active_chat_title(name" in src)
    check("strict 版本不点搜索结果坐标", "wx_human.click(" not in strict_body)
    check("填字走 strict 版本", "wx_chat.open_chat_strict(chat)" in wx_src)
    check("填字侧核对返回值", 'opened.get("ok") is False' in wx_src)

    print(f"RESULT {FAILED} failed")
    return FAILED


if __name__ == "__main__":
    raise SystemExit(main())
