"""工具瘦身开关：YINYUE_TOOLS_DISABLED 摘掉指定工具（可回滚）。

背景：工具 schema 也是每轮 prompt 的一部分 —— 2026-09-22 实测 24 个工具约
11,831 字符，其中三分之一近两天从未被调用。开关默认空 = 全注册。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.tools.base import BaseTool, ToolRegistry  # noqa: E402


class _Fake(BaseTool):  # type: ignore[misc]
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"fake {name}"
        self.parameters = {}

    async def execute(self, **kwargs):  # pragma: no cover - 不执行
        raise NotImplementedError


def _registry(*names: str) -> ToolRegistry:
    reg = ToolRegistry()
    for name in names:
        reg.register(_Fake(name))
    return reg


def test_apply_disabled_removes_only_existing() -> None:
    reg = _registry("a", "b", "c")
    removed = reg.apply_disabled({"a", "nope"})
    assert removed == ["a"]
    assert sorted(reg.all_tools()) == ["b", "c"]


def test_disabled_from_env() -> None:
    os.environ["YINYUE_TOOLS_DISABLED"] = "b, c"
    try:
        reg = _registry("a", "b", "c")
        removed = reg.apply_disabled()
        assert removed == ["b", "c"]
        assert sorted(reg.all_tools()) == ["a"]
    finally:
        os.environ.pop("YINYUE_TOOLS_DISABLED", None)


def test_empty_env_keeps_everything() -> None:
    os.environ["YINYUE_TOOLS_DISABLED"] = ""
    try:
        reg = _registry("a", "b")
        assert reg.apply_disabled() == []
        assert len(reg.all_tools()) == 2
    finally:
        os.environ.pop("YINYUE_TOOLS_DISABLED", None)


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
