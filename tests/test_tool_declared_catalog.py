"""M3 第三刀：模型侧 23 个工具都进了同一份目录（只声明，绑定在 agents 侧）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.tools import declared as D            # noqa: E402
from core.tools.builtin import build_registry   # noqa: E402
from core.tools.invoke import catalog_names     # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok  {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def t_coverage() -> None:
    from agents.tools.base import ToolRegistry as ModelReg

    model = sorted(ModelReg.create_default()._tools.keys())
    catalog = catalog_names()
    missing = [n for n in model if n not in catalog]
    check("模型侧每个工具都在目录里", missing == [], f"缺 {missing}")
    check("目录条数 >= 模型侧条数", len(catalog) >= len(model), f"{len(catalog)} vs {len(model)}")
    check("目录仍含执行器侧工具", {"send_plan", "ui_state", "write_note"} <= catalog)


def t_declared() -> None:
    names = D.declared_names()
    check("声明非空", len(names) >= 8, str(len(names)))
    check("声明与核心侧不重复", not (names & {"send_plan", "web_search", "write_note"}))
    specs = D.declared_specs()
    check("每条声明都能过 validate", all(s.validate()[0] for s in specs),
          str([s.name for s in specs if not s.validate()[0]]))
    check("spec 带 owner", all(s.owner for s in specs))
    by_name = {s.name: s for s in specs}
    for name in ("send_message", "send_sticker", "notify_user"):
        if name in by_name:
            effects = {str(getattr(e, "value", e)) for e in by_name[name].effects}
            check(f"{name} 声明为 external_send", effects == {"external_send"}, str(effects))
    for name in ("create_todo", "complete_todo", "remember"):
        if name in by_name:
            effects = {str(getattr(e, "value", e)) for e in by_name[name].effects}
            check(f"{name} 声明为 write_local", effects == {"write_local"}, str(effects))
    read_like = [s.name for s in specs if {str(getattr(e, "value", e)) for e in s.effects} == {"read"}]
    check("只读类声明存在", len(read_like) >= 8, str(len(read_like)))


def t_registry_merge() -> None:
    registry = build_registry()
    names = {s.name for s in registry.specs()}
    check("目录含声明项", D.declared_names() <= names)
    check("目录无重复名", len(names) == len(registry.specs()))
    dup = [s.name for s in registry.specs() if list(x.name for x in registry.specs()).count(s.name) > 1]
    check("没有同名两份 spec", dup == [], str(dup))


def main() -> int:
    for fn in (t_coverage, t_declared, t_registry_merge):
        print(fn.__name__)
        fn()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
