"""BR-103：console 的 HTTP 入站必须有请求体上限、Content-Length 预判、读超时、必须是 JSON 对象。

守法是**行为**：拿假 handler 打真 `_read_body()`，看它回什么、有没有真的去读 socket。
（旧版按源码字符串断言——那种写法把「常量还在」当成了「这道闸还在」，
而且 do_POST 里再插一次 `rfile.read` 也照样绿。）
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from console import collector  # noqa: E402


class _FakeConn:
    def __init__(self, log):
        self.log = log
        self.timeouts = []

    def settimeout(self, seconds):
        self.timeouts.append(seconds)
        self.log.append("settimeout")


class _FakeReq:
    """`_read_body` 真正用到的那三样：headers / connection / rfile。"""

    def __init__(self, headers=None, data=b""):
        self.log = []
        self.headers = dict(headers or {})
        self.connection = _FakeConn(self.log)
        self._data = data
        self.reads = []

    @property
    def rfile(self):
        return self

    def read(self, n):
        self.reads.append(n)
        self.log.append("read")
        return self._data[:n]


def _req(data=b"{}", **headers):
    h = {"Content-Length": str(len(data))}
    h.update(headers)
    return _FakeReq(h, data)


def test_body_has_hard_cap():
    req = _FakeReq({"Content-Length": str(collector._MAX_BODY + 1)}, b"x" * 32)
    body, err = collector._read_body(req)
    assert body is None
    assert err[0] == 413, err
    assert req.reads == [], "超限时绝不许读 socket"


def test_missing_content_length_is_rejected():
    req = _FakeReq({}, b'{"cmd":"x"}')
    body, err = collector._read_body(req)
    assert body is None, "没有长度就不该猜成空对象"
    assert err[0] == 411, err
    assert req.reads == [], "不知道多大就不许读"


def test_chunked_without_length_is_rejected():
    req = _FakeReq({"Transfer-Encoding": "chunked"}, b"1\r\na\r\n0\r\n\r\n")
    body, err = collector._read_body(req)
    assert body is None and err[0] == 411, err


def test_content_length_must_be_integer():
    req = _FakeReq({"Content-Length": "12x"}, b"{}")
    body, err = collector._read_body(req)
    assert body is None and err[0] == 413, err
    assert req.reads == []


def test_body_must_be_json_object():
    for payload in (b"[]", b'"x"', b"3", b"null"):
        body, err = collector._read_body(_req(payload))
        assert body is None and err[0] == 400, (payload, err)


def test_good_body_comes_back_parsed():
    payload = b'{"cmd":"ping"}'
    req = _req(payload)
    body, err = collector._read_body(req)
    assert err is None
    assert body == {"cmd": "ping"}
    assert req.reads == [len(payload)]


def test_timeout_set_before_reading():
    req = _req(b"{}")
    collector._read_body(req)
    assert req.connection.timeouts == [collector._READ_TIMEOUT]
    assert req.log[:2] == ["settimeout", "read"], req.log


def test_broken_json_is_client_error_not_500():
    body, err = collector._read_body(_req(b"{oops"))
    assert body is None and err[0] == 400, err


def test_do_post_uses_the_single_reader():
    import inspect

    src = inspect.getsource(collector)
    start = src.index("def do_POST(self):")
    handler = src[start:]
    handler = handler[:handler.index("\n        def ")]
    assert "rfile.read" not in handler, "do_POST 里又自己读了一次 body"
    assert "_read_body(self)" in handler
    assert "_MAX_BODY" not in handler, "上限常量现在只有一份，别在 handler 里再定义"
