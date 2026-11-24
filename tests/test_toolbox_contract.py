# -*- coding: utf-8 -*-
"""E3 验收（U-09）：工具包契约 —— effects 分层、注册七字段、注入上限、每回合 ≤2 次。

跑法：``python -X utf8 -u tests/test_toolbox_contract.py``
现在应为 **红**：``core/tools/registry.py`` 只有签名（方法体 raise NotImplementedError）。

规格出处：`docs/decisions/工具包.md` §2 / §4 / §5 / §6 / §7 / §11。
这个切片在 `refactor_plan.yaml` 里**还没有条目**（U-00..U-08 都不覆盖工具包），
所以这份红测试同时也是「加切片」的依据。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.constants import TOOL_IN_PROMPT_MAX, TOOL_MAX_CALLS_PER_TURN  # noqa: E402
from core.tools.registry import (  # noqa: E402
    DEFAULT_EFFECT_MODE,
    TOOL_REASONS,
    Effect,
    ToolRegistry,
    ToolSpec,
)

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}")
        FAILS.append(name)


def spec(name: str = "web_search", **kw) -> ToolSpec:
    base = dict(
        name=name,
        family="web",
        effects=frozenset({Effect.READ}),
        args={"query": "str"},
        returns="results[]",
        perms=(),
        limits={"timeout_s": 8},
        idempotent=True,
        owner="tools/search.py",
    )
    base.update(kw)
    return ToolSpec(**base)


def main() -> int:
    # ── 四种 effects 与默认态（工具包.md §4）────────────────────────
    check("四种 effects 齐全", {e.value for e in Effect} ==
          {"read", "write_local", "ui_control", "external_send"})
    check("read 默认 enabled", DEFAULT_EFFECT_MODE[Effect.READ] == "enabled")
    check("write_local 默认 shadow", DEFAULT_EFFECT_MODE[Effect.WRITE_LOCAL] == "shadow")
    check("ui_control 默认 shadow", DEFAULT_EFFECT_MODE[Effect.UI_CONTROL] == "shadow")
    check("external_send 一律 blocked", DEFAULT_EFFECT_MODE[Effect.EXTERNAL_SEND] == "blocked")
    check("默认未启用（由人打开）", spec().enabled is False)

    # ── 注册七字段自检（工具包.md §5）───────────────────────────────
    # 骨架未实现时这里会 raise NotImplementedError：记成干净 FAIL，不抛 traceback。
    IMPL_ERR = ""
    try:
        ok, _ = spec().validate()
        check("七字段齐 → 可注册", ok is True)
        ok, reason = spec(family="").validate()
        check("缺 family → 拒", ok is False and reason in TOOL_REASONS, f"got=({ok},{reason})")
        ok, reason = spec(owner="").validate()
        check("缺 owner → 拒", ok is False and reason in TOOL_REASONS, f"got=({ok},{reason})")
        many = {f"a{i}": "str" for i in range(5)}
        ok, reason = spec(args=many).validate()
        check("参数 >4 个 → 拒（要拆）", ok is False and reason in TOOL_REASONS,
              f"got=({ok},{reason})")
    except NotImplementedError as exc:
        IMPL_ERR = str(exc)

    reg = ToolRegistry()
    registered = False
    if not IMPL_ERR:
        try:
            reg.register(spec())
            registered = True
        except NotImplementedError as exc:
            IMPL_ERR = str(exc)
    check("register 可注册", registered, IMPL_ERR)

    if not registered:
        for name in ("七字段齐 → 可注册", "缺 family → 拒", "缺 owner → 拒",
                     "参数 >4 个 → 拒（要拆）", "未注册工具名 → unknown_tool",
                     "默认 disabled 不许调", "人工 enable 后可调",
                     "external_send 一律不许直发", "每回合 ≤2 次（含失败）",
                     "注入 prompt ≤6", "注入只含 enabled", "工具名与内部同名方法不混淆"):
            check(name, False, IMPL_ERR or "ToolRegistry 未实现")
        print(f"RESULT {len(FAILS)} failed")
        return 1

    got = reg.get("web_search")
    check("get 取回注册的 spec", got is not None and got.name == "web_search")

    ok, reason = reg.check_call("没注册过的工具", {}, turn_id="t1")
    check("未注册工具名 → unknown_tool", ok is False and reason == "unknown_tool",
          f"got=({ok},{reason})")

    # 默认 disabled 的工具不许被调
    ok, reason = reg.check_call("web_search", {"query": "x"}, turn_id="t1")
    check("默认 disabled 不许调", ok is False and reason in TOOL_REASONS, f"got=({ok},{reason})")

    reg.enable("web_search")
    ok, reason = reg.check_call("web_search", {"query": "x"}, turn_id="t2")
    check("人工 enable 后可调", ok is True, f"got=({ok},{reason})")

    # external_send：即使注册并启用也一律 blocked（出口是唯一对外口）
    reg.register(spec(name="send_msg", family="messaging",
                      effects=frozenset({Effect.EXTERNAL_SEND})))
    reg.enable("send_msg")
    ok, reason = reg.check_call("send_msg", {}, turn_id="t3")
    check("external_send 一律不许直发", ok is False and reason == "external_send_via_exit",
          f"got=({ok},{reason})")

    # 每回合 ≤2 次，失败的也计数
    results = [reg.check_call("web_search", {"query": "q"}, turn_id="t4") for _ in range(3)]
    check("每回合 ≤2 次（含失败）",
          results[0][0] and results[1][0] and results[2] == (False, "too_many_calls"),
          f"got={results}")
    check("次数常量与设计一致", TOOL_MAX_CALLS_PER_TURN == 2)

    # 注入上限 ≤6，只含 enabled
    for i in range(9):
        reg.register(spec(name=f"t{i}"))
    picked = reg.select_for_prompt()
    check("注入 prompt ≤6", len(picked) <= TOOL_IN_PROMPT_MAX == 6, f"got={len(picked)}")
    check("注入只含 enabled", all(s.enabled or s.name == "web_search" for s in picked)
          or all(reg.get(s.name).enabled for s in picked), f"got={[s.name for s in picked]}")

    # ── 同名不同义（工具包.md §11）：core 里已有 observe/schedule，不是工具 ──
    check("工具名与内部同名方法不混淆", reg.get("observe") is None)
    from core.affect import engine as AE  # noqa: E402
    check("core.affect.engine.observe 是内部方法不是 ToolSpec",
          callable(getattr(AE, "observe", None)) and not isinstance(getattr(AE, "observe", None), ToolSpec))

    print(f"RESULT {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
