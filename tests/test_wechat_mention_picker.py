"""@ 一个人：名单走 UIA、唯一同名才点、chip 用「退格整体消失」验证。

为什么值得单测：@ 错人会把通知真的送到别人手机上，所以这四条必须都能拦住：
名单里没有 / 同名多行 / 点完输入框不对 / 退格还剩字（说明只是文字）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.execution as ex
import core.execution.wx_human  # noqa: F401  （先导入，才能替换包属性）
from core.execution import wx_chat as wc

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(cond), detail))


class FakeHuman:
    def __init__(self) -> None:
        self.typed: list[str] = []
        self.clicks: list[tuple[int, int]] = []
        self.keys: list[int] = []

    def type_unicode(self, text: str) -> None:
        self.typed.append(text)

    def click(self, x: int, y: int, **kwargs: object) -> None:
        self.clicks.append((int(x), int(y)))

    def key_press(self, vk: int, **kwargs: object) -> None:
        self.keys.append(int(vk))

    def hotkey(self, *vks: int) -> None:
        return None

    def sleep_human(self, *args: object, **kwargs: object) -> None:
        return None


def compose(*texts: str) -> list[str]:
    """message_box_text 的返回值序列（最后一项会被重复使用）。"""
    return list(texts)


def run_case(
    *,
    items: list[dict],
    composers: list[str],
    target: str = "忽念",
    dry_run: bool = False,
    verify_chip: bool = True,
) -> tuple[dict, FakeHuman, list[int]]:
    human = FakeHuman()
    queue = list(composers)
    cleared: list[int] = []

    def fake_mbt(window=None):  # noqa: ANN001
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0] if queue else ""

    def fake_picker(window=None, _items=items):  # noqa: ANN001
        return list(_items)

    old = (ex.wx_human, wc._wechat_window, wc.focus_message_box, wc.message_box_text,
           wc._picker_items, wc._clear_focused_field)
    ex.wx_human = human  # type: ignore[assignment]
    wc._wechat_window = lambda **kwargs: object()  # type: ignore[assignment]
    wc.focus_message_box = lambda *a, **k: True  # type: ignore[assignment]
    wc.message_box_text = fake_mbt  # type: ignore[assignment]
    wc._picker_items = fake_picker  # type: ignore[assignment]
    wc._clear_focused_field = lambda: cleared.append(1)  # type: ignore[assignment]
    try:
        res = wc.mention_member(target, dry_run=dry_run, verify_chip=verify_chip)
    finally:
        (ex.wx_human, wc._wechat_window, wc.focus_message_box, wc.message_box_text,
         wc._picker_items, wc._clear_focused_field) = old  # type: ignore[assignment]
    return res, human, cleared


def main() -> int:
    ok_items = [{"name": "忽念", "x": 422, "y": 1062}, {"name": "Elaina", "x": 422, "y": 1110}]

    # 1. 唯一同名 → 点它的矩形中心，并通过 chip 验证
    res, human, cleared = run_case(items=ok_items, composers=["@忽念", "", "@忽念"])
    check("唯一同名：成功", res.get("ok") is True, str(res))
    check("唯一同名：via 标成 uia+chip", res.get("via") == "uia+chip", str(res.get("via")))
    check("唯一同名：两次都点 UIA 矩形中心（chip 验证要重选一次）",
          human.clicks == [(422, 1062), (422, 1062)], str(human.clicks))
    check("唯一同名：敲了两次 @名字（chip 验证要重来一次）",
          human.typed == ["@忽念", "@忽念"], str(human.typed))
    check("唯一同名：退格只按一次", human.keys == [0x08], str(human.keys))
    check("唯一同名：没有清空输入框", cleared == [], str(cleared))

    # 2. 名单里没有 → 失败、清空、不点、不回车
    res, human, cleared = run_case(items=ok_items, composers=["@陌生人"], target="陌生人")
    check("名单里没有：失败", res.get("ok") is False, str(res))
    check("名单里没有：点了 0 次", human.clicks == [], str(human.clicks))
    check("名单里没有：清空了输入框", cleared != [], str(cleared))
    check("名单里没有：错误串含目标", "陌生人" in str(res.get("error")), str(res.get("error")))

    # 3. 同名多行 → 不猜
    dup = [{"name": "忽念", "x": 100, "y": 100}, {"name": "忽 念", "x": 200, "y": 200}]
    res, human, cleared = run_case(items=dup, composers=["@忽念"])
    check("同名多行：失败", res.get("ok") is False, str(res))
    check("同名多行：点了 0 次", human.clicks == [], str(human.clicks))
    check("同名多行：错误串说多行", "多行" in str(res.get("error")), str(res.get("error")))

    # 4. 点完输入框不是恰好一个 @名字 → 失败
    res, human, cleared = run_case(items=ok_items, composers=["@忽念@忽念"])
    check("输入框重复：失败", res.get("ok") is False, str(res))
    check("输入框重复：清空了输入框", cleared != [], str(cleared))

    # 5. 退格还剩字（说明是文字不是 chip）→ 失败，且不再重选
    res, human, cleared = run_case(items=ok_items, composers=["@忽念", "@忽"])
    check("退格还剩字：失败", res.get("ok") is False, str(res))
    check("退格还剩字：错误串说不是真 @", "真 @" in str(res.get("error")), str(res.get("error")))
    check("退格还剩字：只点了 1 次（没有重选）", len(human.clicks) == 1, str(human.clicks))

    # 6. dry_run：读名单但不点
    res, human, cleared = run_case(items=ok_items, composers=["@忽念"], dry_run=True)
    check("dry_run：成功", res.get("ok") is True, str(res))
    check("dry_run：标记 dry_run", res.get("dry_run") is True, str(res))
    check("dry_run：点了 0 次", human.clicks == [], str(human.clicks))

    # 7. verify_chip=False 时跳过退格验证（给调用方留一个显式开关）
    res, human, _ = run_case(items=ok_items, composers=["@忽念"], verify_chip=False)
    check("关掉 chip 验证：成功", res.get("ok") is True, str(res))
    check("关掉 chip 验证：不按退格", human.keys == [], str(human.keys))

    # 8. 空目标
    res, _, _ = run_case(items=ok_items, composers=[""], target="  ")
    check("空目标：失败", res.get("ok") is False, str(res))

    failed = [row for row in CHECKS if not row[1]]
    for name, good, detail in CHECKS:
        print(f"{'PASS' if good else 'FAIL'} {name}" + (f" :: {detail}" if not good else ""))
    print(f"RESULT {len(failed)} failed")
    return len(failed)


if __name__ == "__main__":
    raise SystemExit(main())
