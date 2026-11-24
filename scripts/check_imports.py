"""门禁：核心模块必须能 import。

为什么需要它
------------
`py_compile` 只查语法。装饰器冲突、元类冲突、模块级断言这类问题，
**只有类/模块定义真正执行时才会抛**——也就是 import 的时候。

2026-11-02 的事故就是这一类：清理 guess-verify 死代码时删掉了 `GuessGrade` 类，
却漏删了它那行 `@dataclass(frozen=True)`，于是那行落到了下一个类 `Episode` 头上：

    @dataclass(frozen=True)     # 上一轮留下的
    @dataclass(frozen=True)     # Episode 自己的
    class Episode: ...

`frozen=True` 会生成 `__setattr__`，第二个装饰器再写一次直接抛
`TypeError: Cannot overwrite attribute __setattr__ in class Episode`。
py_compile 全绿，调度器却起不来，整条链静默死掉。

用法
----
    python scripts/check_imports.py            # 只跑入口（快，靠依赖树覆盖）
    python scripts/check_imports.py --all      # 逐个跑 core/ loop/ scheduler/（慢，全覆盖）
    python scripts/check_imports.py --verbose  # 打印每个成功的

退出码：0 = 全部可 import；1 = 有失败。
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 入口模块：import 它们会连带把依赖树拉起来，覆盖绝大多数运行时路径。
ENTRY_MODULES = (
    "run",
    "scheduler.main",
    "loop.run",
    "loop.brain",
    "loop.desk",
)

# --all 时扫这些目录
CODE_DIRS = ("core", "loop", "scheduler", "agents", "db", "llm")

# 明确跳过：脚本/临时/测试用，不是运行时模块
SKIP_PARTS = {"_scratch", "__pycache__", "_archive", "tests"}


def _mod_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    return ".".join(rel.parts)


def _collect_all() -> list[str]:
    out: list[str] = []
    for d in CODE_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            if any(part in SKIP_PARTS for part in p.parts):
                continue
            if p.name == "__init__.py":
                continue
            if p.name.startswith("check_") or p.name.startswith("probe_"):
                continue
            out.append(_mod_name(p))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry", action="store_true", help="只跑入口模块（快，但覆盖不到懒加载的模块）")
    ap.add_argument("--verbose", action="store_true", help="打印成功的模块")
    args = ap.parse_args()

    # 2026-11-02：默认改成全量。入口模式的依赖树覆盖不到懒加载模块——
    # 实测把重复装饰器注回 situation.py，入口模式仍然 5/5 全绿，只有全量才报。
    # 全量 459 个模块只要几秒，没有省的必要。
    mods = list(ENTRY_MODULES) if args.entry else _collect_all()
    print("检查 %d 个模块（%s）\n" % (len(mods), "仅入口" if args.entry else "全量"))

    failures: list[tuple[str, str, str]] = []
    ok = 0
    for m in mods:
        try:
            importlib.import_module(m)
            ok += 1
            if args.verbose:
                print("  ok   %s" % m)
        except BaseException as exc:  # noqa: BLE001 - 门禁要抓住一切，包括 SystemExit
            tb = traceback.format_exc(limit=3).strip().splitlines()[-1]
            failures.append((m, type(exc).__name__, tb[:160]))
            print("  FAIL %s" % m)

    print("\n成功 %d / %d" % (ok, len(mods)))
    if failures:
        print("\n失败明细：")
        for m, kind, where in failures:
            print("  %-48s %-20s %s" % (m, kind, where))
        print("\n提示：这类问题 py_compile 查不出来，只有 import 会暴露。")
        return 1
    print("全部可 import。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
