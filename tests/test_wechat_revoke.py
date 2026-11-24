"""撤回：右键 → UIA 读菜单 → 点「撤回」→ 回读确认（U-25）。

跑法：``python -X utf8 -u tests/test_wechat_revoke.py``

两条真机教训钉在这里：
1. 撤回项只在**发出后 2 分钟内**出现（微信自己的窗口），超了菜单里就没有；
2. 菜单是另一个顶层窗口，OCR 读它三次三种结果 —— 菜单项只能靠 UIA。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features  # noqa: E402
from core.execution.gui import wechat_revoke as R  # noqa: E402
from core.execution.gui.base import GuiError  # noqa: E402

FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILED
    if cond:
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name} {detail}")


TEXT = "鲸鲸实验 20:52（一句）"


class FakeDeps:
    """rows 是「屏幕」队列（每次 items() 弹一个），menus 是「菜单」队列。"""

    def __init__(self, *, rows=None, menus=None, title="微信", foreground="微信", open_ok=True):
        self._rows = list(rows or [])
        self._menus = list(menus or [])
        self._title = title
        self._foreground = foreground
        self._open_ok = open_ok
        self.calls: list[tuple] = []
        self._last: list = []

    def window_title(self):
        return self._title

    def foreground_title(self):
        return self._foreground

    def wake(self):
        self.calls.append(("wake",))
        self._foreground = "微信"
        return True

    def open_chat(self, chat):
        self.calls.append(("open_chat", chat))
        return {"ok": self._open_ok, "who": chat, "error": "" if self._open_ok else "搜索验证失败"}

    def items(self):
        self.calls.append(("items",))
        if self._rows:
            self._last = self._rows.pop(0)
        return self._last

    def menu_items(self):
        self.calls.append(("menu",))
        return self._menus.pop(0) if self._menus else []

    def click_right(self, x, y):
        self.calls.append(("right", x, y))

    def click(self, x, y):
        self.calls.append(("click", x, y))

    def cancel(self):
        self.calls.append(("cancel",))

    def sleep(self, seconds):
        self.calls.append(("sleep", round(seconds, 2)))


ROW = (TEXT, 900, 1090, 700, 1079, 0.9)
MENU = [
    ("复制", 1085, 1016),
    ("引用", 1085, 1544),
    ("撤回", 1155, 1500),
]
AFTER_OK = [("你撤回了一条消息", 800, 1150, 700, 1140, 0.8)]


def with_mode(value: str):
    prev = R._mode
    R._mode = lambda: value
    return prev


def with_chain(enabled: bool):
    """老用例都在「屏幕是假的、库是真的」的前提下写，所以把链路关掉走盲翻。"""
    prev = R._use_chain
    R._use_chain = lambda: enabled
    return prev


def test_pure() -> None:
    check("找行：唯一命中", R.find_row([("其它", 1, 1, 0, 0, 0.5), ROW], TEXT) == ROW)
    check("找行：空白不影响", R.find_row([(TEXT.replace(" ", ""), 1, 2, 0, 0, 0.9)], TEXT) is not None)
    check("找行：出现两次 → None（不猜）", R.find_row([ROW, ROW], TEXT) is None)
    check("找行：找不到 → None", R.find_row([("别的", 1, 1, 0, 0, 0.5)], TEXT) is None)
    check("找行：空正文 → None", R.find_row([ROW], "   ") is None)
    check("菜单项：唯一命中", R.find_menu_item(MENU) == MENU[2])
    check("菜单项：没有 → None", R.find_menu_item(MENU[:2]) is None)
    check("菜单项：两项都叫撤回 → None", R.find_menu_item(MENU + [("撤回", 1, 2)]) is None)
    check("菜单项：只认整项相等", R.find_menu_item([("撤回不了", 1, 1)]) is None)
    check("撤回提示能认出来", R._has_revoke_notice(AFTER_OK) is True)
    check("没有提示就是 False", R._has_revoke_notice([("别的消息", 1, 1, 0, 0, 0.5)]) is False)
    check("指纹稳定", R._digest(" a b ") == R._digest("ab"))
    check("常量：菜单项词", R.REVOKE_TOKEN == "撤回")
    check("常量：默认等菜单 0.6s", R.MENU_WAIT_S == 0.6)
    check("常量：回读等 1.0s", R.AFTER_WAIT_S == 1.0)
    check("开关值注册了", features.WECHAT_REVOKE == "wechat_revoke")
    check("开关在三态表里", features.WECHAT_REVOKE in features.TRI_STATE_FLAGS)


def test_happy() -> None:
    prev = with_mode(features.MODE_ON)
    prevc = with_chain(False)
    try:
        d = FakeDeps(rows=[[ROW], AFTER_OK], menus=[MENU])
        r = R.revoke_message("文件传输助手", TEXT, deps=d)
    finally:
        R._mode = prev
        R._use_chain = prevc
        R._use_chain = prevc
        R._use_chain = prevc
        R._use_chain = prevc
    check("成功：status=revoked", r.status == R.STATUS_REVOKED, r.status)
    check("成功：is_revoked", r.is_revoked() is True)
    check("成功：记了菜单项", r.menu_item == "撤回", r.menu_item)
    check("成功：点了右键在那一行", ("right", 900, 1090) in d.calls, str(d.calls))
    check("成功：点了撤回那一项", ("click", 1155, 1500) in d.calls, str(d.calls))
    check("成功：菜单走 menu_items（不靠 OCR）", ("menu",) in d.calls, str(d.calls))
    check("成功：没有 ESC", not any(c[0] == "cancel" for c in d.calls))
    check("成功：先等菜单再回读", [c[0] for c in d.calls].count("sleep") == 2, str(d.calls))
    rec = r.as_receipt()
    check("回执：kind=revoke", rec["kind"] == "revoke", str(rec))
    check("回执：ack=platform_id", rec["ack"] == "platform_id", str(rec))
    check("回执：ref 带指纹", rec["ref"].startswith("wechat-revoke#"), rec["ref"])


def test_fail_closed() -> None:
    prev = with_mode(features.MODE_OFF)
    try:
        try:
            R.revoke_message("x", TEXT, deps=FakeDeps())
            blocked = ""
        except GuiError as exc:
            blocked = exc.code
        check("拒绝：开关关着 → blocked", blocked == "blocked", blocked)
    finally:
        R._mode = prev

    prev = with_mode(features.MODE_ON)
    try:
        for label, chat, text, code in (
            ("没给会话", "", TEXT, "no_target"),
            ("空正文", "文件传输助手", "  ", "unsupported"),
            ("正文过长", "文件传输助手", "字" * 201, "blocked"),
        ):
            try:
                R.revoke_message(chat, text, deps=FakeDeps())
                got = ""
            except GuiError as exc:
                got = exc.code
            check(f"拒绝：{label} → {code}", got == code, got)

        d = FakeDeps(title="微信", foreground="Chrome")
        d.wake = lambda: False
        try:
            R.revoke_message("文件传输助手", TEXT, deps=d)
            got = ""
        except GuiError as exc:
            got = exc.code
        check("拒绝：唤不出微信 → no_target", got == "no_target", got)
        check("拒绝时一次都没点", not any(c[0] in ("right", "click") for c in d.calls), str(d.calls))

        try:
            R.revoke_message("文件传输助手", TEXT, deps=FakeDeps(open_ok=False))
            got = ""
        except GuiError as exc:
            got = exc.code
        check("拒绝：没切到会话 → no_target", got == "no_target", got)

        d = FakeDeps(rows=[[("别的", 1, 1, 0, 0, 0.5)]])
        r = R.revoke_message("文件传输助手", TEXT, deps=d)
        check("找不到这句 → failed/not_found", r.status == R.STATUS_FAILED and "not_found" in r.detail, r.detail)
        check("找不到就不点右键", not any(c[0] == "right" for c in d.calls), str(d.calls))

        d = FakeDeps(rows=[[ROW]], menus=[[("复制", 1, 1), ("删除", 2, 2)]])
        r = R.revoke_message("文件传输助手", TEXT, deps=d)
        check("菜单没撤回 → failed/no_revoke", r.status == R.STATUS_FAILED and "no_revoke" in r.detail, r.detail)
        check("菜单没撤回就 ESC 关掉", ("cancel",) in d.calls, str(d.calls))
        check("菜单没撤回就没点任何项", not any(c[0] == "click" for c in d.calls), str(d.calls))

        d = FakeDeps(rows=[[ROW], [ROW]], menus=[MENU])
        r = R.revoke_message("文件传输助手", TEXT, deps=d)
        check("点了但没变化 → failed/unverified", r.status == R.STATUS_FAILED and "unverified" in r.detail, r.detail)

        d = FakeDeps(rows=[[ROW], []], menus=[MENU])
        r = R.revoke_message("文件传输助手", TEXT, deps=d)
        check("没了但无提示 → failed/unverified", "unverified" in r.detail, r.detail)
    finally:
        R._mode = prev


def test_foreground_wake() -> None:
    prev = with_mode(features.MODE_ON)
    try:
        d = FakeDeps(rows=[[ROW], AFTER_OK], menus=[MENU], foreground="Chrome")
        r = R.revoke_message("文件传输助手", TEXT, deps=d)
        check("前台是别的窗口 → 先唤", d.calls[0][0] == "wake", str(d.calls[:2]))
        check("唤出来后照常撤回", r.status == R.STATUS_REVOKED, r.status)
    finally:
        R._mode = prev


def test_discipline() -> None:
    src = (ROOT / "core/execution/gui/wechat_revoke.py").read_text(encoding="utf-8")
    for banned in ("core.speech", "core.routing", "agents", "scheduler"):
        check(f"撤回层不 import {banned}", f"import {banned}" not in src)
    check("撤回层不直接发消息", "press_enter" not in src and "send_and_verify" not in src)
    check("撤回层不自己算 2 分钟（菜单才是判据）", "REVOKE_WINDOW" not in src)
    check("菜单项经 UIA 读（有 menu_items）", "def menu_items(" in src)
    check("菜单项认 ControlTypeName", "ControlTypeName" in src)
    check("菜单不再靠 OCR", "menu = impl.menu_items()" in src)
    human = (ROOT / "core/execution/wx_human.py").read_text(encoding="utf-8")
    check("wx_human 有 click_menu", "def click_menu(" in human)
    check("click_menu 只此一处", human.count("def click_menu(") == 1)
    seg = human[human.index("def click_menu("):]
    seg = seg[: seg.find("\ndef ")]
    check("click_menu 用 mouse_event 打开菜单（SendInput 不认）", "mouse_event" in seg)
    check("click_menu 仍先 SetCursorPos", "SetCursorPos" in seg)


class ScrollDeps(FakeDeps):
    """会滚轮的那一版假窗口：``scroll`` 记下来，便于断言「翻了几屏、滚回没有」。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.scrolls: list[int] = []

    def scroll(self, delta: int) -> None:
        self.calls.append(("scroll", delta))
        self.scrolls.append(int(delta))


def test_scroll_find() -> None:
    prev = with_mode(features.MODE_ON)
    prevc = with_chain(False)
    try:
        d = ScrollDeps(
            rows=[[("别的消息", 900, 1000, 700, 900, 0.9)], [ROW], AFTER_OK],
            menus=[MENU],
        )
        r = R.revoke_message("鲸鲸的测试群", TEXT, deps=d)
    finally:
        R._mode = prev
    check("翻屏：翻到了就 revoked", r.status == R.STATUS_REVOKED, r.status)
    check("翻屏：第一下往上翻", d.scrolls[:1] == [R.WHEEL_PAGE], str(d.scrolls))
    check("翻屏：撤完滚回原位", d.scrolls[-1] == -R.WHEEL_PAGE, str(d.scrolls))
    check("翻屏：回执写了翻屏数", "翻" in r.detail, r.detail)
    check("翻屏：翻完才右键", [c[0] for c in d.calls].index("scroll") < [c[0] for c in d.calls].index("right"), str(d.calls))


def test_scroll_give_up() -> None:
    prev = with_mode(features.MODE_ON)
    prevc = with_chain(False)
    try:
        d = ScrollDeps(rows=[[("别的消息", 900, 1000, 700, 900, 0.9)]], menus=[])
        r = R.revoke_message("鲸鲸的测试群", TEXT, deps=d)
    finally:
        R._mode = prev
    up = [x for x in d.scrolls if x > 0]
    check("翻不到：failed", r.status == R.STATUS_FAILED, r.status)
    check("翻不到：detail 以 not_found 开头", r.detail.startswith("not_found"), r.detail)
    check("翻不到：屏幕不再变化就停", 0 < len(up) <= R.BLIND_MAX_PAGES, str(d.scrolls))
    check("翻不到：滚回原位", d.scrolls[-1] < 0, str(d.scrolls))
    check("翻不到：一次右键都没点", not any(c[0] == "right" for c in d.calls), str(d.calls))
    check("翻不到：回执说翻了几屏", "翻" in r.detail, r.detail)


def test_scroll_stop_when_stuck() -> None:
    prev = with_mode(features.MODE_ON)
    prevc = with_chain(False)
    try:
        d = ScrollDeps(rows=[[("别的消息", 900, 1000, 700, 900, 0.9)]], menus=[])
        r = R.revoke_message("鲸鲸的测试群", TEXT, deps=d)
    finally:
        R._mode = prev
    up = [x for x in d.scrolls if x > 0]
    check("卡住：只翻一次就停（第二屏和第一屏一样）", len(up) == 1, str(d.scrolls))
    check("卡住：仍然 failed", r.status == R.STATUS_FAILED, r.status)
    check("卡住：还是滚回原位", d.scrolls[-1] < 0, str(d.scrolls))
    check("卡住：指纹能算出来", bool(R._screen_key([("a", 1, 1, 0, 0, 0.5), ("b", 1, 2, 0, 0, 0.5)])))
    check("卡住：空屏指纹是空串", R._screen_key([]) == "")


def test_no_scroll_when_visible() -> None:
    prev = with_mode(features.MODE_ON)
    prevc = with_chain(False)
    try:
        d = ScrollDeps(rows=[[ROW], AFTER_OK], menus=[MENU])
        r = R.revoke_message("鲸鲸的测试群", TEXT, deps=d)
    finally:
        R._mode = prev
    check("屏上就有：一句都不用滚", d.scrolls == [], str(d.scrolls))
    check("屏上就有：revoked", r.status == R.STATUS_REVOKED, r.status)
    check("屏上就有：回执不提翻屏", "翻" not in r.detail, r.detail)


def test_direction() -> None:
    from core.execution.gui import wechat as W

    span = [
        ("00:05 忽念 早", 900, 1000, 700, 900, 0.9),
        ("23:00 忽念 晚", 900, 1200, 700, 900, 0.9),
    ]
    prev = W.message_epoch
    try:
        now = datetime.now()
        W.message_epoch = lambda *a, **k: int(now.replace(hour=0, minute=1, second=0).timestamp())
        check("方向：比屏上最早还早 → 往上（+1）", R._direction_for("c", TEXT, span) == 1)
        W.message_epoch = lambda *a, **k: int(now.replace(hour=23, minute=59, second=0).timestamp())
        check("方向：比屏上最晚还晚 → 往下（-1）", R._direction_for("c", TEXT, span) == -1)
        W.message_epoch = lambda *a, **k: None
        check("方向：库查不到 → 默认往上", R._direction_for("c", TEXT, span) == 1)
        check("钟点：一行都没有 → None", R._visible_span([("没钟点", 1, 1, 0, 0, 0.5)]) is None)
        check("钟点：读出首尾", R._visible_span(span) == (5 * 60, 23 * 3600))
        check("常量：一屏 480 / 盲翻 3 屏 / 每轮 20 屏",
              (R.WHEEL_PAGE, R.BLIND_MAX_PAGES, R.MAX_PAGES_PER_ROUND) == (480, 3, 20))
        check("常量：一行约 5 行/屏", R.ROWS_PER_PAGE == 5.0)
        check("脏数据：非元组行被跳过", R._visible_span([None, 3, "x"]) is None)
    finally:
        W.message_epoch = prev


class ChainDeps(FakeDeps):
    """屏幕是库（chain）的一段滑动窗口；scroll 按 ROWS_PER_PAGE 平移窗口。

    ``start`` 是窗口最上面那行在库里的下标，``delta > 0``（往上翻）让 start 变小。
    """

    def __init__(self, chain, start, size=13, **kw):
        super().__init__(**kw)
        self.chain = chain
        self.start = start
        self.size = size
        self.scrolls = []
        self.revoked = False

    def click(self, x, y):
        super().click(x, y)
        self.revoked = True

    def items(self):
        self.calls.append(("items",))
        if self.revoked:
            return [("你撤回了一条消息", 900, 500, 700, 80, 0.9)]
        part = self.chain[self.start:self.start + self.size]
        return [(r["text"], 900, 100 + 84 * i, 700, 80, 0.9) for i, r in enumerate(part)]

    def scroll(self, delta):
        self.calls.append(("scroll", delta))
        self.scrolls.append(int(delta))
        moved = int(round((int(delta) / R.WHEEL_PAGE) * R.ROWS_PER_PAGE))
        self.start = max(0, min(len(self.chain) - self.size, self.start - moved))


def make_chain(n: int = 200) -> list[dict]:
    return [{"local_id": i + 1, "create_time": 1792500000 + i * 60, "text": f"第{i:03d}条消息", "mine": True}
            for i in range(n)]


def with_chain_rows(chain):
    """把库换成一段假链路（真实实现来自 wechat.recent_rows）。"""
    from core.execution.gui import wechat as W

    prev = W.recent_rows
    W.recent_rows = lambda *a, **k: list(chain)
    return prev


def test_chain_approach() -> None:
    chain = make_chain()
    prev = with_mode(features.MODE_ON)
    prevc = with_chain(True)
    prevr = with_chain_rows(chain)
    try:
        d = ChainDeps(chain, start=180, menus=[MENU])
        r = R.revoke_message("鲸鲸的测试群", "第005条消息", deps=d)
    finally:
        R._mode = prev
        R._use_chain = prevc
        W_recent = None
        from core.execution.gui import wechat as W

        W.recent_rows = prevr
    up = [x for x in d.scrolls if x > 0]
    check("链路：翻到了就 revoked", r.status == R.STATUS_REVOKED, r.status)
    check("链路：一次滚一大步（不是一点一点）", max(up or [0]) >= R.WHEEL_PAGE * 10, str(d.scrolls))
    check("链路：两三轮内停（不空翻）", len(up) <= R.MAX_APPROACH_ROUNDS, str(d.scrolls))
    check("链路：走的是往上（+）", all(x > 0 for x in up), str(d.scrolls))
    check("链路：撤完滚回原位", d.scrolls[-1] < 0, str(d.scrolls))


def test_chain_approach_down() -> None:
    chain = make_chain()
    prev = with_mode(features.MODE_ON)
    prevc = with_chain(True)
    prevr = with_chain_rows(chain)
    try:
        d = ChainDeps(chain, start=5, menus=[MENU])
        r = R.revoke_message("鲸鲸的测试群", "第195条消息", deps=d)
    finally:
        R._mode = prev
        R._use_chain = prevc
        from core.execution.gui import wechat as W

        W.recent_rows = prevr
    check("链路向下：revoked", r.status == R.STATUS_REVOKED, r.status)
    check("链路向下：方向是负（往下翻）", d.scrolls and d.scrolls[0] < 0, str(d.scrolls))


def test_chain_occluded_no_click() -> None:
    chain = make_chain()
    # 目标在库里，但屏幕那一行是媒体占位（永远匹配不上文本）
    chain[5] = {"local_id": 6, "create_time": 1792500030, "text": "", "mine": True}
    prev = with_mode(features.MODE_ON)
    prevc = with_chain(True)
    prevr = with_chain_rows(chain)
    try:
        d = ChainDeps(chain, start=0, menus=[])
        r = R.revoke_message("鲸鲸的测试群", "第005条消息", deps=d)
    finally:
        R._mode = prev
        R._use_chain = prevc
        from core.execution.gui import wechat as W

        W.recent_rows = prevr
    check("被折叠：failed", r.status == R.STATUS_FAILED, r.status)
    check("被折叠：一次右键都没点", not any(c[0] == "right" for c in d.calls), str(d.calls[:8]))


def test_anchor_helpers() -> None:
    rows = [
        {"text": "甲"}, {"text": "乙"}, {"text": "丙"}, {"text": "甲"},
        {"text": "丁"}, {"text": "戊"}, {"text": "己"}, {"text": "庚"},
    ]
    screen = ["乙", "甲", "丁", "戊", "很新的消息"]
    anchors = R._anchors(screen, rows)
    check("锚点：重复行不当锚点（甲 出现两次被跳过）", all(rows[dbi]["text"] != "甲" for _, dbi in anchors), str(anchors))
    check("锚点：只认库里唯一的行", anchors == [(0, 1), (2, 4), (3, 5)], str(anchors))
    check("锚点偏移：取中位数（=2，被跳过的那两行不影响）", R._offset(anchors) == 2, str(R._offset(anchors)))
    check("锚点：空 → None", R._offset([]) is None)
    check("目标下标：唯一才给", R._target_index(rows, "乙") == 1)
    check("目标下标：重复 → None", R._target_index(rows, "甲") is None)
    check("目标下标：没有 → None", R._target_index(rows, "子虚") is None)
    check("比较键：丢空白", R._key(" 第 005 条 ") == "第005条")


def main() -> int:
    test_pure()
    test_happy()
    test_fail_closed()
    test_foreground_wake()
    test_discipline()
    test_scroll_find()
    test_scroll_give_up()
    test_scroll_stop_when_stuck()
    test_no_scroll_when_visible()
    test_direction()
    test_chain_approach()
    test_chain_approach_down()
    test_chain_occluded_no_click()
    test_anchor_helpers()
    print(f"RESULT {FAILED} failed")
    return FAILED


if __name__ == "__main__":
    raise SystemExit(main())
