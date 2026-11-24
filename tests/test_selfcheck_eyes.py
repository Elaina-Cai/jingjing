# -*- coding: utf-8 -*-
"""自检第七节：她眼睛亮不亮（工具清单 + 最近没成的事）。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.execution.gui import lease as L  # noqa: E402
from core.execution.gui import selfcheck as SC  # noqa: E402
from core.tools import builtin  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if ok:
        print("  ok  " + name)
    else:
        FAILS.append(name + " :: " + str(detail))
        print("  FAIL " + name + " :: " + str(detail))


def t_eyes_ok() -> None:
    got = SC._eyes(failures=[])
    check("有清单 = ok", got.ok is True, got.detail)
    check("含 send_plan", got.data.get("has_send_plan") is True, got.data)
    # 2026-10-21（fad588a）UI 工具按效果拆二（ui_state=read / ui=ui_control）：
    # 旧四名并成 2 个，工具总数 10 -> 8。钉下限而不是等值，否则每加一个工具就红一次。
    check("工具数 ≥ 8", int(got.data.get("tools", 0)) >= 8, got.data)
    check("写了字数", "字" in got.detail, got.detail)


def t_eyes_missing() -> None:
    specs = [s for s in builtin.all_specs() if s.name != "send_plan"]
    got = SC._eyes(specs=specs, failures=[])
    check("没有 send_plan = 失败", got.ok is False, got.detail)
    check("点名原因", "send_plan" in got.detail, got.detail)
    empty = SC._eyes(specs=[], failures=[])
    check("空清单 = 失败", empty.ok is False, empty.detail)


def t_eyes_failures() -> None:
    rows = [{"detail": "fill_failed no_target：没找到会话", "status": "no_target"}]
    got = SC._eyes(failures=rows)
    check("报最近没成", "最近没成 1 件" in got.detail, got.detail)
    check("带下一步样本", bool(got.data.get("hint_sample")), got.data)


def t_report_has_seven() -> None:
    r = SC.run_selfcheck(
        capability=lambda: SC.Section("capability", True, "x"),
        backends=lambda: SC.Section("backends", True, "x"),
        eyes=lambda: SC.Section("eyes", True, "x"),
        lease=lambda: SC.Section("lease", True, "x"),
        wechat_db=lambda: SC.Section("wechat_db", True, "x"),
        flags=lambda: SC.Section("flags", True, "x"),
        receipts=lambda: SC.Section("receipts", None, "x"),
    )
    check("七节都在", len(r.sections) == 7, len(r.sections))
    check("眼睛在第三节", [s.name for s in r.sections][2] == "eyes", [s.name for s in r.sections])
    check("整体 ok（➖ 不算失败）", r.ok is True, r.blocked())


def t_lock_is_free() -> None:
    with tempfile.TemporaryDirectory(dir=str(ROOT / ".test_tmp"), ignore_cleanup_errors=True) as d:
        check("空目录 = 锁空闲", L.lock_is_free(L.RESOURCE_INPUT, lock_dir=d) is True)
        held = L.device_lease(L.RESOURCE_INPUT, "probe", lock_dir=d, wait_s=1.0)
        try:
            check("持有中 = 不空闲", L.lock_is_free(L.RESOURCE_INPUT, lock_dir=d) is False)
        finally:
            held.release()
        check("放掉后又空闲", L.lock_is_free(L.RESOURCE_INPUT, lock_dir=d) is True)
        # 真正的"残留"来自非正常退出：状态文件在、锁早就被 OS 回收了。
        residue = Path(d) / "input.json"
        residue.write_text(json.dumps({
            "resource": L.RESOURCE_INPUT, "owner": "ghost", "pid": 1,
            "created_at": 1000.0, "heartbeat_at": 1000.0, "token": "t",
        }), encoding="utf-8")
        got = SC._lease(now=10_000.0, lock_dir=d)
        check("锁空闲 + 状态过期 = 可以开工", got.ok is True, got.detail)
        check("说清是残留", "残留" in got.detail, got.detail)


def main() -> int:
    t_eyes_ok()
    t_eyes_missing()
    t_eyes_failures()
    t_report_has_seven()
    t_lock_is_free()
    print("")
    print("RESULT " + str(len(FAILS)) + " failed / " + str(COUNT) + " checks")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
