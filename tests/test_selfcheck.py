"""U-22 自检报告：六节各说一句人话，`ok is None` 必须是「没测」而不是通过。

钉住的边界：

- 一节炸了不影响其它节（每节独立 try）；
- `None`（没测）不算失败，但也不算通过——`ok` 只看有没有 `False`；
- 开关依赖会报出来（`wechat_send=on` 但 `wechat_ui=off` 这类）；
- 租约持有者心跳过期 = 失败，正常持有 = 「没测」（不是我们该判的）；
- 默认**不连 CDP**（`probe_network=False` 时 backends 里写「未探测」）；
- 措辞：不许说「抱歉」这类话，结论要么「可以开工」要么「先解决 X」。

跑法：.venv_it\\Scripts\\python.exe -X utf8 -u tests/test_selfcheck.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.execution.gui import selfcheck as SC
from core.execution.gui.selfcheck import SECTION_NAMES  # noqa: E402

COUNT = [0]
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    COUNT[0] += 1
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(f"{name} {detail}".strip())
    print(f"  FAIL {name} {detail}".strip())


def sec(name: str, ok, detail: str = "", data=None) -> SC.Section:
    return SC.Section(name=name, ok=ok, detail=detail, data=data or {})


def report(**over) -> SC.SelfCheckReport:
    defaults = {
        "capability": lambda: sec("capability", True, "caps=111111 ok"),
        "backends": lambda: sec("backends", True, "cdp、uia"),
        "lease": lambda: sec("lease", True, "空着"),
        "wechat_db": lambda: sec("wechat_db", True, "1 行可读"),
        "flags": lambda: sec("flags", True, "自洽"),
        "receipts": lambda: sec("receipts", None, "24 小时无回执"),
    }
    defaults.update(over)
    return SC.run_selfcheck(**defaults)


def t_all_green() -> None:
    print("[t_all_green]")
    r = report()
    check("六节齐全", [s.name for s in r.sections] == list(SC.SECTION_NAMES), str([s.name for s in r.sections]))
    check("看起来通过", r.ok is True)
    check("没有失败节", r.blocked() == [])
    check("文字有结论", "结论：这台机器现在可以开工。" in r.text())
    check("dict 形状", set(r.as_dict()) == {"ok", "at", "sections"})
    check("dict 带每节", len(r.as_dict()["sections"]) == len(SECTION_NAMES))
    check("at 有值", bool(r.at))


def t_one_section_fails() -> None:
    print("[t_one_section_fails]")
    r = report(capability=lambda: sec("capability", False, "caps=111101 blocked:composer_unwritable"))
    check("整体失败", r.ok is False)
    check("只报那一节", r.blocked() == ["capability"], str(r.blocked()))
    check("结论点名", "先解决 capability 再动手。" in r.text())
    check("节行带 ❌", "❌ capability" in r.text())
    two = report(capability=lambda: sec("capability", False, "x"), flags=lambda: sec("flags", False, "y"))
    check("多节按序点名", "先解决 capability、flags" in two.text())


def t_none_is_not_pass_nor_fail() -> None:
    print("[t_none_is_not_pass_nor_fail]")
    r = report(receipts=lambda: sec("receipts", None, "未测"))
    check("None 不算失败", r.ok is True)
    check("None 不进 blocked", r.blocked() == [])
    check("行上写 ➖", "➖ receipts" in r.text())
    check("section() 能取到", r.section("receipts") is not None)
    check("section() 取不到给 None", r.section("nope") is None)


def t_flag_dependencies() -> None:
    print("[t_flag_dependencies]")
    modes = {"wechat_send": "on", "wechat_ui": "off"}
    got = SC._flags(reader=lambda n: modes.get(n, "off"))
    check("依赖不满足 = 失败", got.ok is False, got.detail)
    check("点名是哪一对", "wechat_send=on 但 wechat_ui=off" in got.detail, got.detail)
    check("说了为什么", "输入框" in got.detail, got.detail)
    ok_modes = {"wechat_send": "on", "wechat_ui": "on", "gui_channel": "on"}
    good = SC._flags(reader=lambda n: ok_modes.get(n, "off"))
    check("补齐就自洽", good.ok is True, good.detail)
    pair = [p for p in SC.DEPENDENCIES if p[0] == "tool_loop"][0]
    check("工具闭环依赖清单进 prompt", pair[1] == "tool_package_prompt", str(pair))


def t_lease_section() -> None:
    print("[t_lease_section]")
    free = SC._lease(now=1000.0)
    check("空着是 ok", free.ok is True, free.detail)
    # 真的拿一把锁，再让心跳显得很老
    import time

    from core.execution.gui import lease as L
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp(dir=ROOT / ".test_tmp"))
    held = L.device_lease(L.RESOURCE_INPUT, "selfcheck-test", lock_dir=tmp, wait_s=1.0)
    try:
        got = SC._lease(now=time.time(), lock_dir=tmp)
        # 2026-10-21 语义修正：锁**真被占着**就是"现在不能开工"，判 False（原来判 None
        # 是把它当"没测"，于是自检会绿着说"可以开工"）。状态残留但锁空闲 = True。
        check("有人真拿着 = 现在不能开工", got.ok is False, got.detail)
        check("点名持有者", "selfcheck-test" in got.detail, got.detail)
        stale = SC._lease(now=time.time() + 10 * L.DEFAULT_TTL_S, lock_dir=tmp)
        check("心跳过期 = 失败", stale.ok is False, stale.detail)
        check("说了可能僵死", "僵死" in stale.detail, stale.detail)
    finally:
        held.release()
    # 释放之后：状态文件还在（残留），但锁空闲 → 必须判"可以开工"
    after = SC._lease(now=time.time(), lock_dir=tmp)
    check("锁空闲时状态残留不算失败", after.ok is True, after.detail)
    check("说清是残留", "残留" in after.detail, after.detail)


def t_wechat_db_section() -> None:
    print("[t_wechat_db_section]")
    good = SC._wechat_db(reader=lambda: {"path": "x.db", "rows": 1, "latest_age_s": 12.0})
    check("有库 = ok", good.ok is True, good.detail)
    check("报延迟", "12s 前" in good.detail, good.detail)
    none = SC._wechat_db(reader=lambda: {"path": "", "rows": 0})
    check("没库 = 失败", none.ok is False, none.detail)
    boom = SC._wechat_db(reader=lambda: (_ for _ in ()).throw(RuntimeError("锁库了")))
    check("读崩 = 失败但不抛", boom.ok is False and "锁库了" in boom.detail, boom.detail)


def t_probe_crash_isolated() -> None:
    print("[t_probe_crash_isolated]")
    def boom():
        raise RuntimeError("探针炸了")
    r = report(lease=boom)
    check("炸的节记失败", r.section("lease").ok is False)
    check("别的节还在", r.section("capability").ok is True)
    check("说清炸了", "探针炸了" in r.section("lease").detail, r.section("lease").detail)


def t_default_no_network() -> None:
    print("[t_default_no_network]")
    got = SC._backends(probe_network=False)
    check("默认不连 CDP", got.data.get("cdp", {}).get("network") == "未探测（默认不连）", str(got.data.get("cdp")))
    check("两只手都在", set(got.data) >= {"cdp", "uia"}, str(sorted(got.data)))
    check("uia 只会 state", got.data["uia"]["verbs"] == ["state"], str(got.data["uia"]))


def t_receipts_section() -> None:
    print("[t_receipts_section]")
    empty = SC._receipts(counter=lambda: {})
    check("空表 = 没测", empty.ok is None, empty.detail)
    check("空表说还没有回执", "还没有回执" in empty.detail, empty.detail)
    got = SC._receipts(counter=lambda: {"sent": 3, "failed": 1})
    check("有回执 = ok", got.ok is True, got.detail)
    check("按状态列", "sent=3" in got.detail and "failed=1" in got.detail, got.detail)
    boom = SC._receipts(counter=lambda: (_ for _ in ()).throw(RuntimeError("没库")))
    check("查不了 = 没测", boom.ok is None and "没库" in boom.detail, boom.detail)


def t_wording() -> None:
    print("[t_wording]")
    text = report(capability=lambda: sec("capability", False, "x")).text()
    for bad in ("抱歉", "对不起", "失败了哦"):
        check(f"不说「{bad}」", bad not in text)
    src = pathlib.Path("core/execution/gui/selfcheck.py").read_text(encoding="utf-8")
    check("自检不写账本", "terminal_events" not in src and "record_event" not in src)
    check("自检不模拟按键（不 import ctypes、不发合成事件）",
          "import ctypes" not in src and "dispatchMouseEvent" not in src)
    check("CLI 存在", pathlib.Path("scripts/selfcheck.py").exists())


def main() -> int:
    for fn in (
        t_all_green, t_one_section_fails, t_none_is_not_pass_nor_fail, t_flag_dependencies,
        t_lease_section, t_wechat_db_section, t_probe_crash_isolated, t_default_no_network,
        t_receipts_section, t_wording,
    ):
        fn()
    print()
    if FAILS:
        print(f"RESULT {len(FAILS)} failed / {COUNT[0]} checks")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"RESULT 0 failed / {COUNT[0]} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
