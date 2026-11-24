"""CB-004：外部响应的 JSON 解析只走一条路（`egress.parse_json`）。

反例形状（以前会崩的）：
- OneBot 返回登录墙 HTML，`resp.json()` 抛 JSONDecodeError，整条 API 调用链崩；
- 天气接口 200 但 body 是空/HTML，`data["list"][0]` 直接 KeyError。
"""

import glob
import os
import re

import httpx

from core.infra import egress

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME_ROOTS = ("core", "loop", "scheduler", "agents")


def _resp(content: bytes) -> httpx.Response:
    return httpx.Response(200, content=content, request=httpx.Request("GET", "https://example.com/x"))


def test_bad_body_returns_empty_dict():
    assert egress.parse_json(_resp(b"<html>login</html>")) == {}


def test_bad_body_returns_given_default():
    assert egress.parse_json(_resp(b"not json"), default=[]) == []


def test_empty_body_returns_default():
    assert egress.parse_json(_resp(b"")) == {}


def test_valid_json_is_parsed():
    assert egress.parse_json(_resp(b'{"status": "ok", "retcode": 0}'))["retcode"] == 0


def test_accepts_plain_bytes_and_dict():
    assert egress.parse_json(b'{"a": 1}')["a"] == 1
    assert egress.parse_json({"a": 1})["a"] == 1


def test_only_egress_parses_response_json():
    """结构锁：响应解析只许在 egress 里发生（否则"失败退回哪份默认值"就没准了）。"""
    sites = []
    for pkg in RUNTIME_ROOTS:
        for path in glob.glob(os.path.join(ROOT, pkg, "**", "*.py"), recursive=True):
            if "__pycache__" in path:
                continue
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            if rel.endswith("core/infra/egress.py"):
                continue
            for i, line in enumerate(open(path, encoding="utf-8", errors="replace").read().splitlines(), 1):
                if re.search(r"\.json\(\)", line):
                    sites.append("%s:%d" % (rel, i))
    assert sites == []
