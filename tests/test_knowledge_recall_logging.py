# -*- coding: utf-8 -*-
"""task-41 前置补丁验收：knowledge 召回的两个静默出口必须有日志。

背景（task-37 实测，docs/decisions/穷举整改_召回链.md §22）：82 条零命中里
``fact_silent_miss`` 14 条 —— 知识库里当时已有 >=0.2 分的文档，生产却一条没吐；
``core/memory/providers.py`` 的两个出口（``principal is None`` / ``except Exception``）
都是静默 ``return []``，把原因吞掉了。

本用例钉四件事：
1. **正常路径一条新日志都不许写**（off 档零噪声、日志量不被这条放大）；
2. ``principal is None`` 出口写 ``knowledge_recall_silent_skip reason=principal_none``；
3. ``list_all`` 抛异常时写 ``reason=exception`` + ``exc``（repr 截 200 字），
   并且**行为不变**：仍然返回 ``[]``、异常仍然不外泄；
4. 日志本身失败（logger 抛错）不得变成 provider 失败。
"""
from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory import providers as P  # noqa: E402

PASSED = 0
FAILED: list[str] = []

EVENT = "knowledge_recall_silent_skip"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print("PASS", name)
    else:
        FAILED.append(name + (" | " + detail if detail else ""))
        print("FAIL", name, detail)


class _Recorder:
    """替身 logger：只记不写，避免污染生产日志。"""

    def __init__(self, blow: bool = False) -> None:
        self.events: list[tuple[str, dict]] = []
        self.blow = blow

    def warning(self, event="", **kw):
        if self.blow:
            raise RuntimeError("logger exploded")
        self.events.append((str(event), dict(kw)))

    def info(self, event="", **kw):
        self.events.append((str(event), dict(kw)))

    def debug(self, *a, **k):
        pass


class _Doc:
    def __init__(self, id_: int, title: str, content: str) -> None:
        self.id = id_
        self.title = title
        self.content = content
        self.source_url = ""
        self.original_query = ""
        self.source_type = "web"
        self.source_file = ""
        self.created_at = None
        self.updated_at = None


class _Repo:
    def __init__(self, docs: Any, exc: BaseException | None = None) -> None:
        self._docs = docs
        self._exc = exc
        self.calls = 0

    def list_all(self, persona_id=None, limit=100):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return list(self._docs)


def _provider(repo: _Repo) -> P.KnowledgeMemoryProvider:
    return P.KnowledgeMemoryProvider(SimpleNamespace(knowledge_docs=repo))


_DOCS = [
    _Doc(26, "绝区零 E 技能", "游戏里 e 技能怎么按、空技能怎么处理，这里写了一大段说明。"),
    _Doc(61, "小玛复刻时间", "小玛复刻时间是下周三，记得抽卡；复刻时间以公告为准。"),
]

_QUERY = "小玛 复刻 时间"


def _events(rec: _Recorder) -> list[tuple[str, dict]]:
    return [e for e in rec.events if e[0] == EVENT]


def test_normal_path_logs_nothing() -> None:
    rec = _Recorder()
    old = P.log
    P.log = rec
    try:
        items = _provider(_Repo(_DOCS)).recall(object(), _QUERY)
    finally:
        P.log = old
    check("38.正常路径返回命中（>=1 条）", len(items) >= 1, str(len(items)))
    check("38.正常路径不写 knowledge_recall_silent_skip", _events(rec) == [], str(rec.events))
    check("38.正常路径连 info 也不写", rec.events == [], str(rec.events))


def test_principal_none_logs_and_returns_empty() -> None:
    rec = _Recorder()
    repo = _Repo(_DOCS)
    old = P.log
    P.log = rec
    try:
        items = _provider(repo).recall(None, _QUERY)
    finally:
        P.log = old
    ev = _events(rec)
    check("38.principal=None 返回 []", items == [], str(items))
    check("38.principal=None 写且只写一条日志", len(ev) == 1, str(ev))
    check("38.principal_none 的 reason 字段", ev and ev[0][1].get("reason") == "principal_none", str(ev))
    check("38.principal=None 不打库（list_all 未被调用）", repo.calls == 0, str(repo.calls))
    check("38.principal_none 的 exc 为空串", ev and ev[0][1].get("exc") == "", str(ev))


def test_exception_logs_and_is_still_swallowed() -> None:
    rec = _Recorder()
    boom = RuntimeError("no such column: summary")
    repo = _Repo(_DOCS, exc=boom)
    old = P.log
    P.log = rec
    try:
        items = _provider(repo).recall(object(), _QUERY)
    finally:
        P.log = old
    ev = _events(rec)
    check("38.异常路径仍然返回 []（行为不变）", items == [], str(items))
    check("38.异常路径不外泄异常", True)
    check("38.异常路径写且只写一条日志", len(ev) == 1, str(ev))
    payload = ev[0][1] if ev else {}
    check("38.exception 的 reason 字段", payload.get("reason") == "exception", str(payload))
    exc_field = str(payload.get("exc") or "")
    check("38.exc 含异常类型与消息", "RuntimeError" in exc_field and "no such column" in exc_field, exc_field)
    check("38.exc 截断到 200 字以内", len(exc_field) <= 200, str(len(exc_field)))


def test_fields_truncated() -> None:
    rec = _Recorder()
    long_q = "长" * 300
    old = P.log
    P.log = rec
    try:
        _provider(_Repo(_DOCS)).recall(None, long_q)
        _provider(_Repo(_DOCS, exc=RuntimeError("x" * 500))).recall(object(), _QUERY)
    finally:
        P.log = old
    ev = _events(rec)
    check("38.query 截断到 80 字以内", all(len(str(e[1].get("query") or "")) <= 80 for e in ev), str(ev))
    check("38.exc 最多 200 字", all(len(str(e[1].get("exc") or "")) <= 200 for e in ev), str(ev))


def test_logger_failure_never_becomes_provider_failure() -> None:
    rec = _Recorder(blow=True)
    old = P.log
    P.log = rec
    try:
        items = _provider(_Repo(_DOCS)).recall(None, _QUERY)
        ok = True
    except Exception:  # noqa: BLE001
        items, ok = [], False
    finally:
        P.log = old
    check("38.logger 抛错不影响返回值", ok and items == [], "raised=%s" % (not ok))


def test_source_exits_are_instrumented() -> None:
    src = (ROOT / "core" / "memory" / "providers.py").read_text(encoding="utf-8")
    check("38.源码含 _log_knowledge_silent_skip 定义", "def _log_knowledge_silent_skip(" in src)
    check("38.principal_none 分支已插桩",
          'if principal is None:\n            _log_knowledge_silent_skip(reason="principal_none", query=query)\n            return []'
          in src)
    check("38.exception 分支已插桩（仍 return []）",
          'except Exception as exc:  # noqa: BLE001' in src
          and '_log_knowledge_silent_skip(reason="exception", query=query, exc=exc)\n            return []' in src)
    check("38.没有把异常重新抛出", "raise" not in src[src.index('_log_knowledge_silent_skip(reason="exception"'):][:200])


def main() -> int:
    test_normal_path_logs_nothing()
    test_principal_none_logs_and_returns_empty()
    test_exception_logs_and_is_still_swallowed()
    test_fields_truncated()
    test_logger_failure_never_becomes_provider_failure()
    test_source_exits_are_instrumented()
    if FAILED:
        print("\nFAILED %d / %d" % (len(FAILED), len(FAILED) + PASSED))
        for name in FAILED:
            print(" -", name)
        return 1
    print("\nALL PASSED (%d)" % PASSED)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
