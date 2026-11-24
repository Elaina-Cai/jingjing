"""egress 的 request 钩子（`_guard`）必须守 check() 的现行契约。

背景（踩坑档案 #20）：`check()` 早期是「能出 → 返回 None，不能出 → 返回原因字符串」，
后来改成「不能出 → 抛 Blocked，能出 → 返回主机名」。`_guard` 还写着老契约的
`why = check(...)` + `if why: raise Blocked(why, url)`，于是**每个通过的请求都被自己拒掉**：
报错是 `Blocked('127.0.0.1', 'http://127.0.0.1:.../x.png')`，看着像「策略不允许」，其实是契约用反了。

这里用真起一个本机 HTTP 服务做行为锁：local 车道能过，public 车道的明文 http 回环不能过。
两个方向都要锁：同步钩子喂给 `AsyncClient` 会报 `NoneType can\'t be awaited`；
异步钩子喂给同步 `Client` 则**静默不拦**（协程没人 await）——后者没有任何报错，只能被测出来。
"""

from __future__ import annotations

import ast
import http.server
from pathlib import Path
import threading

import pytest

from core.infra import egress


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - 标准库命名
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def loopback_url():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d/probe" % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_local_lane_can_reach_loopback_plain_http(loopback_url: str) -> None:
    with egress.client(lane="local", timeout_ms=2_000) as client:
        resp = client.get(loopback_url)
    assert resp.status_code == 200


def test_public_lane_still_refuses_loopback_plain_http(loopback_url: str) -> None:
    with pytest.raises(egress.Blocked):
        with egress.client(lane="public", timeout_ms=2_000) as client:
            client.get(loopback_url)


def test_async_local_lane_can_reach_loopback_plain_http(loopback_url: str) -> None:
    import asyncio

    async def go() -> int:
        async with egress.async_client(lane="local", timeout_ms=2_000) as client:
            resp = await client.get(loopback_url)
            return resp.status_code

    assert asyncio.run(go()) == 200

@pytest.mark.parametrize(
    "lane, explicit, want",
    [
        ("local", False, True),
        ("public", False, False),
        ("public", True, True),
        ("local", True, True),
    ],
)
def test_allow_private_is_one_derivation(lane: str, explicit: bool, want: bool) -> None:
    """车道 -> 能不能走明文 http 回环，只有一份判据（HO-013）。"""
    assert egress.allow_private_for(lane, explicit) is want


def test_allow_private_lane_test_lives_in_one_place() -> None:
    """`lane == 'local'` 这种判断在 egress 里只许出现一次（且就在 allow_private_for 里）。

    用 AST 数比较节点，不数文本：docstring 里提名字不算，注释里写反例也不算。
    """
    src = (Path(__file__).resolve().parents[2] / "core" / "infra" / "egress.py").read_text(
        encoding="utf-8"
    )
    compares = [
        node.lineno
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "lane"
        and any(isinstance(c, ast.Constant) and c.value == "local" for c in node.comparators)
    ]
    assert len(compares) == 1, f"lane=='local' 的比较有 {len(compares)} 处：{compares}"
