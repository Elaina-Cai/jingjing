"""出网唯一出口的规则测试（DSH-045）。

这些测试只用本机回环服务器，不出公网。
"""

import asyncio
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.infra import egress


class _H(BaseHTTPRequestHandler):
    hits = 0
    payload = b"hello"
    redirect = None

    def do_GET(self):
        type(self).hits += 1
        if type(self).redirect:
            self.send_response(302)
            self.send_header("Location", type(self).redirect)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = type(self).payload
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/x" % srv.server_address[1]


def test_bad_scheme_and_host_blocked():
    for bad in ("http://example.com/a", "ftp://example.com", "https://", "http://10.0.0.5/a"):
        try:
            egress.check(bad)
        except egress.Blocked:
            continue
        raise AssertionError("应当拦住：" + bad)


def test_credentials_in_url_blocked():
    try:
        egress.check("https://user:pw@example.com/a")
    except egress.Blocked:
        return
    raise AssertionError("带账号密码应当拦住")


def test_public_ip_passes():
    assert egress.check("https://1.1.1.1/x") == "1.1.1.1"


def test_loopback_needs_permission():
    try:
        egress.check("http://127.0.0.1:8000/x")
    except egress.Blocked:
        pass
    else:
        raise AssertionError("明文 http 到回环也要 allow_private")
    assert egress.check("http://127.0.0.1:8000/x", allow_private=True) == "127.0.0.1"


def test_budget_only_shrinks():
    assert egress.budget_ms(None) == egress.TOTAL_TIMEOUT_MS
    assert egress.budget_ms(10 ** 9) == egress.TOTAL_TIMEOUT_MS
    assert egress.budget_ms(1000) == 1000
    assert egress.budget_ms(0) == 1


def test_redirect_not_followed():
    srv, url = _serve()
    _H.hits = 0
    _H.redirect = "http://127.0.0.1:1/elsewhere"
    try:
        reply = egress.get(url, allow_private=True)
        assert reply.status == 302, reply.status
        assert reply.body == b""
        assert _H.hits == 1, _H.hits
    finally:
        _H.redirect = None
        srv.shutdown()


def test_body_cap():
    srv, url = _serve()
    _H.payload = b"x" * 5000
    try:
        try:
            egress.get(url, allow_private=True, max_bytes=1024)
        except egress.TooLarge:
            pass
        else:
            raise AssertionError("超过上限应当抛 TooLarge")
        assert egress.get(url, allow_private=True, max_bytes=9000).body == b"x" * 5000
    finally:
        _H.payload = b"hello"
        srv.shutdown()


def test_success_sync_and_async():
    srv, url = _serve()
    try:
        reply = egress.get(url, allow_private=True)
        assert reply.ok and reply.text == "hello", reply.text
        areply = asyncio.run(egress.aget(url, allow_private=True))
        assert areply.text == "hello"
    finally:
        srv.shutdown()


def test_describe_and_redact():
    assert egress.describe("https://a.com/p/q?token=abc#frag") == "https://a.com/p/q"
    out = egress.redact("key=abcd1234&x=1 Authorization: Bearer abcdefgh12345678 sk-abcdefgh1234")
    for leaked in ("abcd1234", "abcdefgh12345678", "sk-abcdefgh1234"):
        assert leaked not in out, leaked
