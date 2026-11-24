"""出网唯一出口（DSH-045）。

改这个文件之前先读完这七条——它们是这个模块存在的理由：

1. 只有本模块允许 import 网络客户端（httpx / requests / aiohttp / urllib.request）。
   scripts/check.py 的 egress 规则会拦；新代码要出网就调这里的函数。
2. 目的地按"性质"判，不按黑名单判：主机解析出来的每一个地址都必须是公网单播
   （ipaddress.ip_address(x).is_global）。回环、内网、链路本地、组播、保留段一律拒——
   所以以后新增一个内网段、换一台云主机，这里都不需要改。
3. 只走 https。allow_private=True 时才允许明文 http，并且只对本机回环放行
   （本机服务：CDP、Ollama、QQ 桥接）。
4. 永不跟随重定向：3xx 原样返回，要不要再发一次由调用方决定。
   （跟随重定向等于把"目的地检查"交给对方，SSRF 就从这个口子进来。）
5. 响应体按块读，累计上限 MAX_BODY_BYTES，超了抛 TooLarge；不静默截断，
   因为静默截断会让人以为"拿到了完整页面"。
6. 总时限 TOTAL_TIMEOUT_MS，连接+传输一起算。调用方只能把它调小，不能调大。
7. 日志只写 describe(url) 和 redact(text)：query/fragment 整体丢掉、密钥一律打码。
   不写 headers（header 里就是 key）。

已知残留：校验时解析一次、httpx 真正连接时再解析一次，中间存在 DNS rebinding 的理论窗口。
要彻底消掉得自己建 TLS 连接（SNI+证书校验按域名做），代价远大于收益，先记在这里。
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlsplit

import httpx

from core.infra import duration

MAX_BODY_BYTES = 256 * 1024 * 1024   # 2026-10-31 取消上限（原来 2MB）
TOTAL_TIMEOUT_MS = 35_000
CONNECT_TIMEOUT_MS = 10_000
CHUNK_BYTES = 64 * 1024
LOCAL_TIMEOUT_MS = 180_000  # 内网车道天花板（本机后端跑长任务）
USER_AGENT = "Mozilla/5.0 (compatible; YinyueBot/1.0)"
_LOOPBACK_NAMES = ("localhost", "localhost.localdomain", "ip6-localhost")
_SECRET_KEYS = (
    "key", "api_key", "apikey", "token", "access_token", "refresh_token",
    "secret", "client_secret", "signature", "sig", "password", "passwd", "auth", "code",
)


def _warn(event: str, **fields: Any) -> None:
    """延迟导入 safelog：避免 core <-> loop 的模块级循环。"""
    try:
        from loop.safelog import warn
    except Exception:
        return
    try:
        warn(event, **fields)
    except Exception:
        return


class EgressError(Exception):
    code = "egress_error"


class Blocked(EgressError):
    code = "blocked"


class TooLarge(EgressError):
    code = "too_large"


class TimedOut(EgressError):
    code = "timeout"


class Upstream(EgressError):
    code = "upstream"

    def __init__(self, status: int, url: str, body: bytes = b"") -> None:
        super().__init__("HTTP %d" % status)
        self.status = status
        self.url = url
        self.body = body


@dataclass
class Reply:
    status: int
    url: str
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    elapsed_ms: int = 0

    @property
    def text(self) -> str:
        charset = "utf-8"
        ctype = str(self.headers.get("content-type", "")).lower()
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def is_redirect(self) -> bool:
        return self.status in (301, 302, 303, 307, 308)


def describe(url: str) -> str:
    """日志里唯一允许出现的 URL 形态：丢掉 query 与 fragment。"""
    try:
        parts = urlsplit(str(url))
    except Exception:
        return "<bad-url>"
    host = parts.hostname or ""
    port = (":" + str(parts.port)) if parts.port else ""
    path = parts.path or "/"
    return parts.scheme + "://" + host + port + path


def redact(text: str) -> str:
    """把密钥类内容打码。写日志前必须过一遍。"""
    out = str(text or "")
    out = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-***", out)
    out = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1***", out)
    keys = "|".join(re.escape(k) for k in _SECRET_KEYS)
    out = re.sub(r"(?i)((?:%s)=)[^&\s\"']+" % keys, r"\1***", out)
    out = re.sub(r"(?i)((?:%s)\"\s*:\s*\")[^\"]{8,}" % keys, r"\1***", out)
    out = re.sub(r"\b[A-Fa-f0-9]{32,}\b", "***", out)
    return out


def _resolve(host: str) -> list:
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return []
    addrs = []
    for info in infos:
        addr = info[4][0]
        if addr not in addrs:
            addrs.append(addr)
    return addrs


def _is_loopback(host: str, addrs: Iterable) -> bool:
    if (host or "").lower() in _LOOPBACK_NAMES:
        return True
    got = list(addrs)
    if not got:
        return False
    for addr in got:
        try:
            if not ipaddress.ip_address(addr.split("%")[0]).is_loopback:
                return False
        except ValueError:
            return False
    return True


def allow_private_for(lane: str, allow_private: bool = False) -> bool:
    """这一条就是「谁能走明文 http 回环」的唯一判据，别再在调用点写一遍。

    为什么抽出来：`request` / `arequest` / 客户端钩子原来各写一份 `lane == 'local'`，
    再加一条车道就会漏一处；漏的那种不是报错，而是悄悄放宽或悄悄收紧。

    语义：显式 `allow_private=True` 仍有效（老调用点兼容），`lane='local'` 等价放开，
    其余一律收紧——默认收紧才是安全方向。
    """
    return bool(allow_private or lane == 'local')


def check(url: str, *, allow_private: bool = False) -> str:
    """校验一个 URL 能不能出网：能出返回主机名，不能出抛 Blocked。"""
    parts = urlsplit(str(url or ""))
    if parts.scheme not in ("http", "https"):
        raise Blocked("只允许 http/https：" + describe(url))
    if parts.username or parts.password:
        raise Blocked("URL 里不许带账号密码")
    host = (parts.hostname or "").strip()
    if not host:
        raise Blocked("URL 没有主机名")
    addrs = _resolve(host)
    if not addrs:
        raise Blocked("解析不到主机：" + host)
    loop = _is_loopback(host, addrs)
    if parts.scheme == "http" and not (allow_private and loop):
        raise Blocked("明文 http 只允许本机回环（出公网请用 https）")
    for addr in addrs:
        ip = ipaddress.ip_address(addr.split("%")[0])
        if allow_private and (ip.is_loopback or ip.is_private):
            continue
        if not ip.is_global:
            raise Blocked("目的地不是公网地址：%s -> %s" % (host, addr))
    return host


def budget_ms(timeout_ms: int | None = None, *, lane: str = 'public') -> int:
    # 天花板按车道分：出网有硬上限（一次调用不许拖着不放）；
    # 本机后端可能跑长任务（工具执行 120s），那是内网车道，不该被出网天花板掐断。
    ceiling = TOTAL_TIMEOUT_MS if lane == 'public' else LOCAL_TIMEOUT_MS
    if timeout_ms is None:
        return ceiling
    return max(1, min(int(timeout_ms), ceiling))


def _default_headers(headers: dict | None) -> dict:
    merged = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    for key, value in (headers or {}).items():
        merged[str(key)] = str(value)
    return merged


def _timeout_for(budget: int):
    return httpx.Timeout(budget / 1000.0, connect=min(CONNECT_TIMEOUT_MS, budget) / 1000.0)


def _finish(reply: Reply, url: str) -> Reply:
    if reply.status >= 400:
        _warn("egress_upstream_error", url=describe(url), status=reply.status,
              elapsed_ms=reply.elapsed_ms)
        raise Upstream(reply.status, url, reply.body)
    return reply


def request(method: str, url: str, *, headers: dict | None = None, params: dict | None = None,
            json_body: Any = None, content: bytes | None = None,
            allow_private: bool = False, timeout_ms: int | None = None,
            lane: str = 'public',
            max_bytes: int | None = None, raise_for_status: bool = True) -> Reply:
    """同步出网。规矩全在模块头那七条里。"""
    allow_private = allow_private_for(lane, allow_private)
    check(url, allow_private=allow_private)
    cap = int(max_bytes) if max_bytes else MAX_BODY_BYTES
    budget = budget_ms(timeout_ms, lane=lane)
    started = duration.now_ms()
    deadline = started + budget
    with httpx.Client(follow_redirects=False, trust_env=False, timeout=_timeout_for(budget)) as client:
        try:
            with client.stream(str(method).upper(), url, params=params, json=json_body,
                               content=content, headers=_default_headers(headers)) as resp:
                reply = Reply(status=resp.status_code, url=url, headers=dict(resp.headers))
                if not reply.is_redirect:
                    chunks = []
                    total = 0
                    for chunk in resp.iter_bytes(CHUNK_BYTES):
                        total += len(chunk)
                        if total > cap:
                            _warn("egress_too_large", url=describe(url), bytes=total, cap=cap)
                            raise TooLarge("响应超过上限 %d 字节：%s" % (cap, describe(url)))
                        if duration.now_ms() > deadline:
                            _warn("egress_timeout", url=describe(url), bytes=total)
                            raise TimedOut("读取超时：" + describe(url))
                        chunks.append(chunk)
                    reply.body = b"".join(chunks)
        except (TooLarge, TimedOut):
            raise
        except httpx.TimeoutException as exc:
            _warn("egress_timeout", url=describe(url), err=type(exc).__name__)
            raise TimedOut("超时：" + describe(url)) from None
        except httpx.HTTPError as exc:
            _warn("egress_failed", url=describe(url), err=type(exc).__name__)
            raise EgressError("%s: %s" % (type(exc).__name__, describe(url))) from None
    reply.elapsed_ms = duration.now_ms() - started
    if reply.is_redirect:
        _warn("egress_redirect_refused", url=describe(url), status=reply.status)
        return reply
    return _finish(reply, url) if raise_for_status else reply


async def arequest(method: str, url: str, *, headers: dict | None = None, params: dict | None = None,
                   json_body: Any = None, content: bytes | None = None,
                   allow_private: bool = False, timeout_ms: int | None = None,
                   lane: str = 'public',
                   max_bytes: int | None = None, raise_for_status: bool = True) -> Reply:
    """异步出网。语义与 request 完全一致（只多一个 await）。"""
    allow_private = allow_private_for(lane, allow_private)
    check(url, allow_private=allow_private)
    cap = int(max_bytes) if max_bytes else MAX_BODY_BYTES
    budget = budget_ms(timeout_ms, lane=lane)
    started = duration.now_ms()
    deadline = started + budget
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False,
                                 timeout=_timeout_for(budget)) as client:
        try:
            async with client.stream(str(method).upper(), url, params=params, json=json_body,
                                     content=content, headers=_default_headers(headers)) as resp:
                reply = Reply(status=resp.status_code, url=url, headers=dict(resp.headers))
                if not reply.is_redirect:
                    chunks = []
                    total = 0
                    async for chunk in resp.aiter_bytes(CHUNK_BYTES):
                        total += len(chunk)
                        if total > cap:
                            _warn("egress_too_large", url=describe(url), bytes=total, cap=cap)
                            raise TooLarge("响应超过上限 %d 字节：%s" % (cap, describe(url)))
                        if duration.now_ms() > deadline:
                            _warn("egress_timeout", url=describe(url), bytes=total)
                            raise TimedOut("读取超时：" + describe(url))
                        chunks.append(chunk)
                    reply.body = b"".join(chunks)
        except (TooLarge, TimedOut):
            raise
        except httpx.TimeoutException as exc:
            _warn("egress_timeout", url=describe(url), err=type(exc).__name__)
            raise TimedOut("超时：" + describe(url)) from None
        except httpx.HTTPError as exc:
            _warn("egress_failed", url=describe(url), err=type(exc).__name__)
            raise EgressError("%s: %s" % (type(exc).__name__, describe(url))) from None
    reply.elapsed_ms = duration.now_ms() - started
    if reply.is_redirect:
        _warn("egress_redirect_refused", url=describe(url), status=reply.status)
        return reply
    return _finish(reply, url) if raise_for_status else reply


def get(url: str, **kwargs: Any) -> Reply:
    return request("GET", url, **kwargs)


def post_json(url: str, payload: Any, **kwargs: Any) -> Reply:
    kwargs.setdefault("json_body", payload)
    return request("POST", url, **kwargs)


async def aget(url: str, **kwargs: Any) -> Reply:
    return await arequest("GET", url, **kwargs)


async def apost_json(url: str, payload: Any, **kwargs: Any) -> Reply:
    kwargs.setdefault("json_body", payload)
    return await arequest("POST", url, **kwargs)

# ── 客户端工厂：策略跟着客户端走 ──────────────────────────────
# 为什么要有它：出网策略写在调用点，就等于靠每个调用者自觉（我们踩过）。
# 这里把校验器绑到 httpx 的 request 事件上——凡是用这个客户端发出去的请求，
# 无论调用点怎么写都会被同一套规则检查；重定向不跟随（3xx 直接回报）。
def _check_request(request, lane: str) -> None:
    """钩子的实体：check 失败时自己抛 Blocked，成功时返回主机名。

    别沿用老契约的 `why = check(...)` + `if why: raise`——check 现在「能出返回主机名」，
    那样写会把每个通过的请求都拒掉。
    """
    check(str(request.url), allow_private=allow_private_for(lane))


def _guard(lane: str):
    """同步 client 的 request 钩子。"""

    def hook(request) -> None:
        _check_request(request, lane)

    return hook


def _aguard(lane: str):
    """异步 client 的 request 钩子。

    httpx 的 AsyncClient 是 `await hook(request)`，喂它同步函数会变成 `await None`
    （报 `TypeError: 'NoneType' object can't be awaited`）。两个形状各一个，策略共用一处。
    """

    async def hook(request) -> None:
        _check_request(request, lane)

    return hook


def async_client(*, lane: str = 'public', timeout_ms: int | None = None,
                 headers: dict | None = None):
    budget = budget_ms(timeout_ms)
    return httpx.AsyncClient(
        timeout=_timeout_for(budget),
        headers=_default_headers(headers),
        follow_redirects=False,
        event_hooks={'request': [_aguard(lane)]},
    )


def client(*, lane: str = 'public', timeout_ms: int | None = None,
           headers: dict | None = None):
    budget = budget_ms(timeout_ms)
    return httpx.Client(
        timeout=_timeout_for(budget),
        headers=_default_headers(headers),
        follow_redirects=False,
        event_hooks={'request': [_guard(lane)]},
    )

def connect(host: str, port: int, *, timeout_ms: int | None = None):
    """开一条 TCP 连接（只许本机/内网）。

    为什么要有它：本机服务（执行器 39872、TTS、CDP）不是 HTTP 客户端，
    但它们同样是“开一条出去的连接”。门只能有一扇，否则策略就是摆设。
    """
    import socket as _socket

    target = str(host or '').strip()
    # check() 成功时返回的是**主机名**，不是原因（契约：能出返回主机名，不能出抛 Blocked）。
    # 2026-10-30 之前这里把返回值当原因用，于是每一条 TCP 连接都被自己判成 Blocked。
    check('http://%s:%d/' % (target or '0.0.0.0', int(port)), allow_private=True)
    budget = budget_ms(timeout_ms, lane='local')
    return _socket.create_connection((target, int(port)), timeout=budget / 1000.0)



def parse_json(resp: Any, default: Any = None) -> Any:
    """把一条已到手的响应解析成 JSON；解析不了就回 `default`（不给就是 `{}`）。

    CB-004 的另一半：上游「HTTP 200 但不是 JSON」（登录墙、限流页、被劫持的
    HTML）不该把调用方崩掉，也不该逼每个调用点各抄一遍
    `try: resp.json() except: ...`。解析失败的形状只有一种，所以判定只放一处。

    `default` 按调用点的语义给：字典接口 `{}`（默认），列表接口 `[]`。
    """
    fallback = {} if default is None else default
    if isinstance(resp, (dict, list)):
        return resp
    if isinstance(resp, (bytes, bytearray, str)):
        body: Any = resp
    else:
        body = None
        for attr in ("content", "body"):
            got = getattr(resp, attr, None)
            if got is not None:
                body = got() if callable(got) else got
                break
    if body is None:
        got = getattr(resp, "text", None)
        body = got() if callable(got) else got
    try:
        if isinstance(body, str):
            return json.loads(body)
        return json.loads(bytes(body or b"").decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - 解析失败只有一种形状：这条没有 JSON
        _warn("egress_bad_json", url=describe(getattr(resp, "url", "")), err=type(exc).__name__)
        return fallback
