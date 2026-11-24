"""DSH-045 两条锁：总时限真的会到点；出网的门是“形状”判据，不是关键字清单。

这两条原来都没有——`test_egress.py` 只证了 `budget_ms` 会收缩（参数层面），
没人证过“body 一滴滴喂也能被掐断”；`check_egress` 只做字符串包含，
`import httpx as hx` 能绕过，注释里写一句 `httpx.Client(` 又会误报。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from core.infra import egress

ROOT = Path(__file__).resolve().parents[2]


def _load_check():
    spec = importlib.util.spec_from_file_location("_check_egress_script", ROOT / "scripts" / "check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


check = _load_check()


class _Clock:
    """假时钟：每读一块就走一格，用来看“到点”而不是“等真时间”。"""

    def __init__(self, step_ms: int) -> None:
        self.now = 0
        self.step = step_ms

    def __call__(self) -> int:
        return self.now

    def tick(self) -> None:
        self.now += self.step


class _Resp:
    status_code = 200
    headers = {}

    def __init__(self, chunks: int, clock: _Clock) -> None:
        self._chunks = chunks
        self._clock = clock

    def iter_bytes(self, size):
        for _ in range(self._chunks):
            self._clock.tick()
            yield b"x" * 8

    async def aiter_bytes(self, size):
        for _ in range(self._chunks):
            self._clock.tick()
            yield b"x" * 8


class _Stream:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    def __enter__(self) -> _Resp:
        return self._resp

    def __exit__(self, *exc) -> bool:
        return False

    async def __aenter__(self) -> _Resp:
        return self._resp

    async def __aexit__(self, *exc) -> bool:
        return False


class _Client:
    def __init__(self, resp: _Resp, **kw) -> None:
        self._resp = resp

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def stream(self, *a, **kw) -> _Stream:
        return _Stream(self._resp)


def _wire(monkeypatch, chunks: int, step_ms: int) -> _Clock:
    clock = _Clock(step_ms)
    monkeypatch.setattr(egress.duration, "now_ms", clock)
    monkeypatch.setattr(egress, "check", lambda *a, **kw: None)
    resp = _Resp(chunks, clock)
    monkeypatch.setattr(egress.httpx, "Client", lambda **kw: _Client(resp))
    monkeypatch.setattr(egress.httpx, "AsyncClient", lambda **kw: _Client(resp))
    return clock


def test_sync_deadline_stops_a_dripping_body(monkeypatch):
    _wire(monkeypatch, chunks=20, step_ms=200)
    with pytest.raises(egress.TimedOut):
        egress.request("GET", "https://example.com/x", timeout_ms=600)


def test_async_deadline_stops_a_dripping_body(monkeypatch):
    _wire(monkeypatch, chunks=20, step_ms=200)
    with pytest.raises(egress.TimedOut):
        asyncio.run(egress.arequest("GET", "https://example.com/x", timeout_ms=600))


def test_short_body_is_not_mistaken_for_a_timeout(monkeypatch):
    _wire(monkeypatch, chunks=2, step_ms=10)
    reply = egress.request("GET", "https://example.com/x", timeout_ms=600)
    assert reply.status == 200
    assert reply.body == b"x" * 16


def test_checker_sees_alias_import():
    assert check._egress_offences("import httpx as hx\n\nhx.Client()\n", "x.py") == ["x.py:3 httpx.Client"]


def test_checker_sees_from_import():
    bad = check._egress_offences("from httpx import Client\n\nClient()\n", "x.py")
    assert bad and bad[0].startswith("x.py:3 ")


def test_checker_ignores_comments_and_exception_use():
    src = "import httpx\n\n# httpx.Client(...) 只是注释\ndef f(e):\n    return isinstance(e, httpx.HTTPError)\n"
    assert check._egress_offences(src, "x.py") == []


def test_checker_respects_noqa():
    src = 'import socket\n\nsocket.create_connection(("127.0.0.1", 1))  # noqa: egress\n'
    assert check._egress_offences(src, "x.py") == []


def test_repo_has_no_second_door():
    assert check.check_egress() == []
