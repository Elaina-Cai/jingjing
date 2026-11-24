"""M3 第二刀：工具调用唯一入口 + 模型侧目录覆盖影子。"""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.tools import invoke as I  # noqa: E402

FAILS: list[str] = []
COUNT = 0


class FakeResult:
    def __init__(self, status: str = "ok") -> None:
        self.status = status
        self.output = "x"


class FakeSpec:
    def __init__(self, effects) -> None:
        self.effects = effects


class FakeRegistry:
    def __init__(self, effects=("read",)) -> None:
        self.calls: list[tuple] = []
        self.spec = FakeSpec(effects)

    def get(self, name: str):
        if name == "missing":
            raise KeyError(name)
        return self.spec

    def observe(self, name, what, params=None):
        self.calls.append(("observe", name))
        return FakeResult()

    def act(self, name, action, params=None):
        self.calls.append(("act", name))
        return FakeResult()


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok  {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def t_routing() -> None:
    reg = FakeRegistry(("read",))
    I.invoke(reg, "web_search", {"q": "x"}, turn_id="t1")
    check("只读走 observe", reg.calls == [("observe", "web_search")])

    reg = FakeRegistry(("external_send",))
    I.invoke(reg, "send_plan", {"target": "群"})
    check("外发走 act", reg.calls == [("act", "send_plan")])

    reg = FakeRegistry(("read",))
    I.invoke(reg, "ui_state", mode="act")
    check("mode=act 可强制", reg.calls == [("act", "ui_state")])

    reg = FakeRegistry(("external_send",))
    I.invoke(reg, "send_plan", mode="observe")
    check("mode=observe 可强制", reg.calls == [("observe", "send_plan")])

    reg = FakeRegistry(("read", "write_local"))
    I.invoke(reg, "write_note")
    check("混合效果按写处理", reg.calls == [("act", "write_note")])

    reg = FakeRegistry(("quantum",))
    I.invoke(reg, "weird")
    check("不认识的效果按读处理（不猜写）", reg.calls == [("observe", "weird")])


def t_errors() -> None:
    reg = FakeRegistry(("read",))
    try:
        I.invoke(reg, "missing")
    except KeyError:
        check("未知工具按原样抛", True)
    else:
        check("未知工具按原样抛", False)

    class Boom(FakeRegistry):
        def observe(self, name, what, params=None):
            raise RuntimeError("炸了")

    try:
        I.invoke(Boom(("read",)), "web_search")
    except RuntimeError:
        check("工具异常向上抛", True)
    else:
        check("工具异常向上抛", False)


def t_logs() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("core.tools.invoke")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        I.invoke(FakeRegistry(("read",)), "web_search", turn_id="t9", target="私聊")
        text = stream.getvalue()
    finally:
        logger.removeHandler(handler)
    check("日志含 tool_invoke", "tool_invoke" in text)
    check("日志带 via", "via=observe" in text)
    check("日志带 turn", "turn=t9" in text)


def t_catalog_coverage() -> None:
    names = I.catalog_names()
    check("目录里有 send_plan", "send_plan" in names)
    check("目录里有 ui_state", "ui_state" in names)
    check("covered_by_catalog 认 send_plan", I.covered_by_catalog("send_plan"))
    check("covered_by_catalog 拒瞎写", not I.covered_by_catalog("rm_rf"))


def t_agent_shadow_static() -> None:
    src = (ROOT / "agents" / "base.py").read_text(encoding="utf-8")
    check("影子日志存在", "tool_catalog_shadow" in src)
    check("影子包在 try 里", "try:  # M3 影子" in src)
    check("影子只 debug 失败", 'log.debug(f"tool_catalog_shadow_failed' in src)
    start = src.find("try:  # M3 影子")
    end = src.find("async def ", start)
    body = src[start:end]
    check("影子不调 act/observe", "registry.act(" not in body and "registry.observe(" not in body)


def main() -> int:
    for fn in (t_routing, t_errors, t_logs, t_catalog_coverage, t_agent_shadow_static):
        print(fn.__name__)
        fn()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
