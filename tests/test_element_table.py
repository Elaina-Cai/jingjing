"""元素表的硬要求：只认唯一命中、代次作废、来源优先级、矩形是屏幕坐标。

跑法：.venv_it\\Scripts\\python.exe -X utf8 -u tests/test_element_table.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.execution.gui import elements as E  # noqa: E402

FAILS: list[str] = []
COUNT = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    COUNT[0] += 1
    if cond:
        print(f"  ok  {name}")
    else:
        FAILS.append(f"{name} :: {detail}")
        print(f"  FAIL {name}  {detail}")


class FakeBackend:
    """两窗口：主窗（有「发送」「取消」「发送」三个同名）+ 弹窗（有「张三」）。"""

    def __init__(self, *, boom: bool = False) -> None:
        self.boom = boom
        self.tree_calls: list[int] = []

    def windows(self):
        if self.boom:
            raise RuntimeError("windows failed")
        return [
            {"hwnd": 100, "title": "微信", "class": "Qt51514QWindowIcon",
             "rect": (0, 0, 1289, 1537), "active": True},
            {"hwnd": 200, "title": "Weixin", "class": "Qt51514QWindowToolSaveBits",
             "rect": (260, 996, 587, 1176), "active": False},
            {"hwnd": 0, "title": "无句柄的窗口", "class": "x", "rect": (0, 0, 1, 1)},
        ]

    def tree(self, hwnd, *, max_depth=12):
        self.tree_calls.append(hwnd)
        if hwnd == 100:
            return [
                {"role": "ButtonControl", "name": "发送", "rect": (1216, 1476, 1265, 1505)},
                {"role": "ButtonControl", "name": "发送", "rect": (1216, 1600, 1265, 1630)},
                {"role": "ButtonControl", "name": "取消", "rect": (1100, 1476, 1200, 1505)},
                {"role": "EditControl", "name": "", "rect": (390, 1137, 1267, 1300)},
            ]
        if hwnd == 200:
            return [
                {"role": "ListItemControl", "name": "张三", "rect": (301, 1038, 544, 1086)},
            ]
        raise RuntimeError("no such window")


def el(name, rect=(0, 0, 10, 10), *, role="", window=100, source=E.SOURCE_UIA):
    return E.Element(name=name, role=role, rect=rect, source=source, window=window)


def main() -> int:
    print("[1] 扫一遍：窗口过滤、空名丢弃、无句柄窗口跳过")
    backend = FakeBackend()
    result = E.scan_once(backend=backend, generation=7)
    check("只扫两个有句柄的窗口", backend.tree_calls == [100, 200], str(backend.tree_calls))
    check("元素总数 4（两条「发送」都保留，空名那条被丢）", len(result.elements) == 4, str(len(result.elements)))
    check("dropped=1", result.dropped == 1, str(result.dropped))
    check("代次带出来", result.generation == 7 and result.table().generation == 7)
    check("统计按来源", result.source_stats == {"uia": 4}, str(result.source_stats))
    check("窗口矩形是屏幕绝对坐标", result.window_of(100).rect == (0, 0, 1289, 1537))

    print("[2] 唯一命中：0 个、多个都返回 None")
    table = result.table()
    check("取消 唯一", table.unique("取消") is not None)
    check("发送 有两个 -> None", table.unique("发送") is None, str(len(table.find("发送"))))
    check("找不到 -> None", table.unique("不存在的按钮") is None)
    check("空名字 -> 空列表", table.find("") == [] and table.find("   ") == [])
    check("find 返回 2 条且按面积降序", [e.rect[1] for e in table.find("发送")] == [1600, 1476])

    print("[3] 跨顶层窗口：弹窗里的元素查得到（这就是「找不着」的 A 类）")
    hit = table.unique("张三")
    check("张三 在弹窗 200 上", hit is not None and hit.window == 200, str(hit and hit.window))
    check("window= 过滤生效", table.unique("张三", window=100) is None)
    check("by_window 只给该窗口的", len(table.by_window(200)) == 1)

    print("[4] 角色过滤 + 模糊匹配")
    check("按角色找", table.unique("张三", role="ListItemControl") is not None)
    check("角色不符 -> None", table.unique("张三", role="ButtonControl") is None)
    check("模糊命中 取消", table.unique("取", exact=False) is not None)

    print("[5] 合并同来源/跨来源：uia 优先于 ocr")
    ocr_copy = el("张三", role="ListItemControl", rect=(302, 1039, 545, 1087), window=200, source=E.SOURCE_OCR)
    merged = E.merge_elements(table.elements, [ocr_copy])
    picks = [e for e in merged if e.name == "张三"]
    check("同名只留一条", len(picks) == 1, str(len(picks)))
    check("留下的是 uia 的矩形", picks[0].source == E.SOURCE_UIA and picks[0].rect[0] == 301, str(picks[0].as_dict()))
    dom_same = el("取消", role="ButtonControl", rect=(1101, 1477, 1201, 1506), source=E.SOURCE_DOM)
    merged2 = E.merge_elements([dom_same], table.elements)
    kept = [e for e in merged2 if e.name == "取消"]
    check("同一颗按钮只留一条", len(kept) == 1, str(len(kept)))
    check("留下的是优先级高的 uia", kept[0].source == E.SOURCE_UIA, kept[0].source)
    far = el("取消", role="ButtonControl", rect=(0, 0, 10, 10), source=E.SOURCE_OCR)
    check("位置差很远的同名元素不算同一个", len([e for e in E.merge_elements([far], table.elements) if e.name == "取消"]) == 2)

    print("[6] 代次：不一致整表作废 / 不能合并")
    check("代次相同 -> 视为当前", table.is_current(7))
    check("代次不同 -> 作废", not table.is_current(8))
    raised = False
    try:
        table.merge(E.ElementTable([el("x")], generation=8))
    except ValueError:
        raised = True
    check("跨代次合并报 ValueError", raised)

    print("[7] 预算：超过上限截断并报数")

    class ManyBackend:
        def windows(self):
            return [{"hwnd": 1, "title": "w", "class": "c", "rect": (0, 0, 10, 10)}]

        def tree(self, hwnd, *, max_depth=12):
            return [{"name": f"按钮{i}", "rect": (0, i, 10, i + 10)} for i in range(30)]

    cut = E.scan_once(backend=ManyBackend(), max_elements=10)
    check("截断到 10", len(cut.elements) == 10, str(len(cut.elements)))
    check("truncated=20", cut.truncated == 20, str(cut.truncated))
    check("summary 可序列化", cut.summary()["elements"] == 10)

    print("[8] 后端炸了不连坐")
    bad = E.scan_once(backend=FakeBackend(boom=True))
    check("windows 抛异常 -> 空结果", bad.elements == [] and bad.windows == [])

    print("[9] Element 自身的账：center / usable")
    check("center 取矩形中心", el("x", (10, 20, 30, 40)).center == (20, 30))
    check("零面积不可用", not el("x", (5, 5, 5, 5)).usable)
    check("空名不可用", not el("   ").usable)

    print()
    print(f"RESULT {len(FAILS)} failed / {COUNT[0]} checks")
    for line in FAILS:
        print("  -", line)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
