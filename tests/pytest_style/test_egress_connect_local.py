"""`egress.connect()` 对本地监听端口不许抛：`check()` 返回的是主机名，不是错误。

2026-10-30：`core/infra/egress.py::connect` 把 ``check()`` 的返回值当错误判断
（``why = check(...); if why: raise Blocked(why)``），而 ``check()`` 成功时返回主机名，
于是连 ``127.0.0.1`` 都抛 ``Blocked: ('127.0.0.1','127.0.0.1')``，面板和采集器之间
永远连不上。这条用例把「连得上就返回 socket」钉住。
"""
from __future__ import annotations

import socket

import pytest

from core.infra import egress


def _listener() -> tuple[socket.socket, int]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


def test_connect_to_local_listener_returns_socket() -> None:
    srv, port = _listener()
    try:
        sock = egress.connect("127.0.0.1", port, timeout_ms=2000)
        assert isinstance(sock, socket.socket)
        sock.close()
    finally:
        srv.close()


def test_connect_to_closed_port_raises_oserror_not_blocked() -> None:
    srv, port = _listener()
    srv.close()
    with pytest.raises(OSError) as boom:
        egress.connect("127.0.0.1", port, timeout_ms=1000)
    assert not isinstance(boom.value, egress.Blocked)
