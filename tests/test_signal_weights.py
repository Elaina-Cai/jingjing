# -*- coding: utf-8 -*-
"""死字段 / 死权重守卫：``signals.topic_similarity``（权重 0.10）已退役。

背景（``docs/架构审计/C-理解与判定.md`` B13、``docs/架构所有权地图.md:49``、
``docs/decisions/架构评估.md:379`` X3）：

    ``topic_similarity`` 曾是 ``AddresseeSignals`` 的字段、``_WEIGHTS`` 里的 0.10，
    但**全仓没有任何生产者**：``extract_signals`` 的形参默认 0.0，
    ``AddresseeResolver.evaluate`` 的形参默认 0.0，唯一的调用方直接透传默认值，
    所以它**恒 0**。带权重的死字段比死代码更坏：读代码的人会以为"话题相似度在
    加分"，实际那条判据从来没生效过。

退役口径与守卫（三段）：

* ① 字段 / 形参 / 权重键 / 归一化四处**全部消失**，且 ``_score`` 不再有那一项；
* ② 静态守卫：``core/`` 下**不许再出现** ``topic_similarity`` 这个名字（AST 扫
   Name / Attribute / keyword / 字符串常量——注释不算，注释里保留退役说明是有意的）；
* ③ 泛化守卫：``_WEIGHTS`` 的每个键都必须是 ``AddresseeSignals`` 的真实字段
  （否则就是**下一个**死权重）；并带一次"扫描器自检"，防止守卫退化成空断言。

重新引入它的正确姿势：先有 owner（N3 焦点状态 ``core/context/threads.py``）提供
簇重叠度，再由**生产者**把它传进来——别再放一个恒 0 的默认值。

Run directly: python tests/test_signal_weights.py
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent

#: 退役的死字段名（全仓只许出现在注释/文档里）。
_RETIRED_NAME = "topic_similarity"
#: 扫描根：能把它**喂回来**的产线代码（core / agents / web）。
#: `tests/` 不在内——测试要能写出"它不该存在"的对照夹具；`scripts/` 也不在内——
#: 那里只有文档生成器 `draw_arch_diagrams.py` 用**散文**提到这个退役名（"死字段退役"），
#: 不是生产者（实测 scripts/ 里 `same_sender` / `reply_to_bot` 这类权重名 0 命中）。
_SCAN_ROOTS = ("core", "agents", "web")
_SCAN_SKIP_DIRS = frozenset({"__pycache__", "build", "site-packages", "_scratch"})


# ----------------------------------------------------------------- ① 已删除
def test_retired_signal_is_gone_from_field_param_and_weight() -> None:
    """①字段、形参、权重键、``as_dict`` 输出四处都没有它了。"""
    from core.conversation.resolver import _WEIGHTS, AddresseeResolver
    from core.conversation.signals import AddresseeSignals, extract_signals

    fields = set(AddresseeSignals.__dataclass_fields__)
    assert _RETIRED_NAME not in fields, f"死字段又回来了：{sorted(fields)}"
    assert _RETIRED_NAME not in AddresseeSignals().as_dict(), AddresseeSignals().as_dict()

    import inspect

    for func in (extract_signals, AddresseeResolver.evaluate):
        params = inspect.signature(func).parameters
        assert _RETIRED_NAME not in params, f"{func.__qualname__} 还收它：{list(params)}"

    assert _RETIRED_NAME not in _WEIGHTS, (
        f"死权重又回来了（它恒 0，有生产者再放）：{sorted(_WEIGHTS)}"
    )
    assert _RETIRED_NAME not in AddresseeResolver(apply_overrides=False).weights


def test_retired_weight_no_longer_contributes_to_the_score() -> None:
    """①′分数只由有生产者的信号决定：只有一个信号时，分数 == 它的权重。"""
    from core.conversation.resolver import AddresseeResolver
    from core.conversation.signals import AddresseeSignals

    resolver = AddresseeResolver(apply_overrides=False)
    score = resolver._score(AddresseeSignals(window_open=True))
    assert score == 0.05, f"除 window_open(0.05) 外还有别的项在加分：{score}"


# ----------------------------------------------------------------- ② 静态守卫
def _retired_name_sites() -> list[tuple[str, int, str]]:
    """``core/`` 等产品代码里引用退役名字的位置（文件、行号、写法）。"""
    found: list[tuple[str, int, str]] = []
    for root in _SCAN_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            if _SCAN_SKIP_DIRS & set(path.parts):
                continue
            rel = path.relative_to(ROOT).as_posix()
            source = path.read_text(encoding="utf-8-sig")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    if node.id == _RETIRED_NAME:
                        found.append((rel, node.lineno, "name"))
                elif isinstance(node, ast.Attribute):
                    if node.attr == _RETIRED_NAME:
                        found.append((rel, node.lineno, "attr"))
                elif isinstance(node, ast.keyword):
                    if node.arg == _RETIRED_NAME:
                        found.append((rel, node.lineno, "keyword"))
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    # 权重表 / 配置键是字符串常量：字符串里出现也算（含前缀写法）。
                    if _RETIRED_NAME in node.value:
                        found.append((rel, node.lineno, "constant"))
    return found


def test_retired_name_is_absent_from_product_code() -> None:
    """②``core/`` 等产品代码里不许再有这个名字（注释里的退役说明不算）。"""
    sites = _retired_name_sites()
    assert not sites, (
        f"{'/、'.join(_SCAN_ROOTS)}/ 下又出现了退役死字段 {_RETIRED_NAME}：{sites}"
    )


def test_scan_guard_detects_an_injected_weight() -> None:
    """②′守卫自检：把死字段塞回一段源码，扫描器必须命中（否则守卫是空断言）。"""
    source = (
        "from core.conversation.signals import extract_signals\n"
        "def f(signals):\n"
        "    return extract_signals(None, topic_similarity=0.5)\n"
    )
    names = {
        node.arg for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.keyword)
    }
    assert _RETIRED_NAME in names, names
    weights = ast.parse('W = {"topic_similarity": 0.10}').body[0].value
    keys = {
        node.value for node in ast.walk(weights)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert _RETIRED_NAME in keys, keys


# ----------------------------------------------------------------- ③ 泛化守卫
def test_every_weight_key_is_a_real_signal_field() -> None:
    """③权重表的每个键都必须是 ``AddresseeSignals`` 的字段 = 没有下一个死权重。"""
    from core.conversation.resolver import _WEIGHTS
    from core.conversation.signals import AddresseeSignals

    fields = set(AddresseeSignals.__dataclass_fields__)
    orphans = sorted(set(_WEIGHTS) - fields)
    assert not orphans, (
        f"权重表里有 AddresseeSignals 没有的键（拿了权重却读不到值 = 死权重）：{orphans}"
    )
    assert _WEIGHTS, "权重表空了？守卫会退化成空断言"


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
        except Exception as exc:  # noqa: BLE001 - 脚本模式下也要看清是哪条炸了
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
