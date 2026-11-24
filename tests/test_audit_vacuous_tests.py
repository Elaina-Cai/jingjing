# -*- coding: utf-8 -*-
"""空转体检工具自测（1989bb8）：工具自己也得有人看着。

四件事：①静态候选只收 tests/ 下「有 test_*/t_* 可调用但没 __main__ 入口」的文件；
②已接好入口的文件（有 main() + 守卫）不许被当成候选；③run_one 对「断言失败」的
文件必须报非 0、对「真跑起来」的文件必须拿到输出——否则这个工具只会永远报 OK；
④**名单里的 10 个文件必须仍然「可被判定为已接好入口」**：文件还在、`__main__` 守卫还在、
仍然走 ``tests/_mini_pytest.run_all``、且不在候选里。

第 ④ 条是 2026-10-22 补的，补的是一个真实的漏洞：老版本只断言「这 12 个名字不出现在
候选里」——于是把文件**删掉或改名**之后，它不再是候选，断言反而更绿。名单变成了
「只减不增、还不用解释」的静默豁免，正是这个仓库反复踩的那类病（表里记的东西和现实
脱钩，脱钩时没人被叫醒）。现在改成「名单里的每一个都必须自证还活着且仍被守卫」。

为什么不在用例里真跑这 12 个：全量里它们本来就是子进程里的子进程，且这些文件用
``tests/_tempdirs`` 的隔离目录，套跑容易互相污染；「跑起来到底有没有输出」由
``scripts/audit_vacuous_tests.py --run`` 覆盖（report-only，未接门禁）。
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from _tempdirs import make_db_dir  # noqa: E402
import audit_vacuous_tests as AV  # noqa: E402

#: 2026-10-22 补过入口的 10 个文件（曾长期空转）。它们必须**永远仍然活着**：
#: 文件在、`__main__` 守卫在、仍走 `_mini_pytest.run_all`、且不在候选里。
#: 删掉/改名会让 `t_fixed_files_are_still_guarded` 变红——这才是「名单里的文件失败则红」。
_FIXED = {
    "test_act_view.py",
    "test_intention_runner.py",
    "test_judgment_layer_boundary.py", "test_no_wechat_leak_in_common_layers.py",
    "test_position_block.py", "test_scene_read_facts.py", "test_shadow_report.py",
    "test_tool_dry_load.py", "test_utterance_edge_time.py",
}

#: `__main__` 守卫（兼容单双引号；不锚定行首的缩进，文件级守卫按约定在行首）。
_MAIN_GUARD = re.compile(r"""^if __name__ == ['"]__main__['"]:\s*$""", re.MULTILINE)

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


def t_candidates_are_guarded_less_files() -> None:
    got = AV.static_candidates()
    names = {p.name for p in got}
    # 不收「非空」这种会随仓库状态变化的断言：2026-10-22 把 12 个空转文件都补了入口之后，
    # 正确状态就是**空列表**（见 tests/_mini_pytest.py）。这里只锁"分类函数的语义"。
    check(
        "候选都确实没有 __main__ 入口",
        all(
            'if __name__ == "__main__":' not in p.read_text(encoding="utf-8", errors="replace")
            for p in got
        ),
        [p.name for p in got],
    )
    check("候选都在 tests/", all(p.parent == AV.TESTS for p in got))
    check("有入口的文件不是候选", "test_selfcheck_eyes.py" not in names, sorted(names)[:5])
    check("补过入口的 10 个文件一个都不在候选里", not (names & _FIXED), sorted(names)[:5])


def t_fixed_files_are_still_guarded() -> None:
    """名单里的 10 个文件必须仍然存在、仍然有 `__main__` 入口、仍然走统一 runner。

    老版本只查「不在候选里」⇒ 删掉/改名之后它不再是候选，断言反而更绿（静默豁免）。
    现在逐个自证：文件在 + 入口在 + runner 在 + 不是候选；缺任意一项 ⇒ 红。
    """
    cands = {p.name for p in AV.static_candidates()}
    missing: list[str] = []
    no_entry: list[str] = []
    no_runner: list[str] = []
    back_vacuous: list[str] = []
    for name in sorted(_FIXED):
        path = AV.TESTS / name
        if not path.is_file():
            missing.append(name)
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        if not _MAIN_GUARD.search(src):
            no_entry.append(name)
        if "_mini_pytest" not in src:
            no_runner.append(name)
        if name in cands:
            back_vacuous.append(name)
    check(
        "名单里的文件都还在（删/改名会静默变绿，这也是本用例存在的理由）",
        not missing,
        "以下文件不在了："
        + str(missing)
        + " —— 修法：要么把它加回 tests/ 并补入口，要么从 _FIXED 里摘掉，"
        "并在提交信息里写明「这个文件为什么可以不跑」",
    )
    check("名单里的文件都还有 __main__ 入口", not no_entry, no_entry)
    check("名单里的文件都仍走 tests/_mini_pytest.run_all", not no_runner, no_runner)
    check("名单里的文件没有退回空转候选", not back_vacuous, back_vacuous)


def t_run_one_sees_failure() -> None:
    td = make_db_dir("audit_vacuous_")
    try:
        good = Path(td) / "t_good.py"
        good.write_text("print('  ok  ran')\n", encoding="utf-8")
        bad = Path(td) / "t_bad.py"
        bad.write_text("assert False, 'boom'\n", encoding="utf-8")
        r_good = AV.run_one(good)
        r_bad = AV.run_one(bad)
        check(
            "真跑起来的不算空转",
            r_good.get("vacuous") is False and int(r_good.get("output_chars", 0)) > 0,
            r_good,
        )
        check("断言失败报非 0", int(r_bad.get("exit", 0)) != 0, r_bad)
    finally:
        shutil.rmtree(td, ignore_errors=True)


TESTS = [t_candidates_are_guarded_less_files, t_fixed_files_are_still_guarded, t_run_one_sees_failure]


def main() -> bool:
    ok = True
    for test in TESTS:
        print(test.__name__)
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("  FAIL " + test.__name__ + " :: " + repr(exc))
    print("")
    if FAILS:
        ok = False
        print("FAILED " + str(len(FAILS)) + ":")
        for f in FAILS:
            print("  - " + f)
    else:
        print("audit_vacuous_tests checks: " + str(COUNT) + "/" + str(COUNT) + " passed")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
