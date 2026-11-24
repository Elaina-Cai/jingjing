"""声明式工具：加一个能力 = 加一个 YAML 文件（阶段 4 能力面）。

一个 ``configs/tools/*.yaml`` 只描述一件**只读**的「去看一眼」::

    name: wttr_weather
    family: web
    effects: [read]
    args: {city: str}
    returns: 当前天气摘要
    limits: {timeout_s: 8, max_chars: 600}
    allow_hosts: [wttr.in]
    http:
      method: GET
      url: "https://wttr.in/{city}?format=3"

五条纪律（每条都有一条测试钉住）：

1. **只声明，不执行任意代码**：没有 shell、没有模板表达式、没有 ``eval``。
2. **效果白名单**：只许 ``read`` / ``write_local``。``external_send`` 一律拒——出口唯一，
   工具包不许自己开口说话；``ui_control`` 也拒——它要三条判据（前台/标题/像素），
   YAML 表达不了，硬写就是骗自己。
3. **主机白名单必填**：``allow_hosts`` 为空 = 拒绝加载；URL 模板的主机部分**不许**带
   占位符（``https://{host}/x`` 一律拒），否则白名单等于没有。
4. **参数上限**：沿用 ``TOOL_ARGS_SPLIT_OVER``（>4 个就拆成两个工具）。
5. **默认不启用**：文件里 ``enabled: true`` **与**调用方 ``enable=True`` 同时成立才 ``enable()``；
   只登记不启用时，它连 prompt 都进不去。
"""

from __future__ import annotations

import io
import logging
import urllib.error
import urllib.parse
import urllib.request
from core.infra import duration, egress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from core.tools.registry import Effect, ToolRegistry, ToolResult, ToolSpec
from safelog import warn

log = logging.getLogger(__name__)

#: 默认目录（相对仓库根）。
TOOL_DIR = "configs/tools"

#: 文件名后缀。
SUFFIXES: tuple[str, ...] = (".yaml", ".yml")

#: 允许声明的效果。**故意没有** ``external_send`` 与 ``ui_control``。
ALLOWED_EFFECTS: frozenset[Effect] = frozenset({Effect.READ, Effect.WRITE_LOCAL})

#: 今天只支持 GET：写请求要幂等键与回读，那不是一个 YAML 能做对的事。
ALLOWED_METHODS: tuple[str, ...] = ("GET",)

DEFAULT_TIMEOUT_S = 8.0
DEFAULT_MAX_CHARS = 600
MAX_PLACEHOLDERS = 8


class DeclarativeToolError(ValueError):
    """加载失败。``reason`` 是稳定的机器可读串（测试直接断言它）。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class HttpReply:
    """一次 HTTP 读的结果。``status == 0`` 表示没拿到 HTTP 状态（超时/连不上）。"""

    status: int = 0
    text: str = ""
    error: str = ""


@dataclass(frozen=True)
class DeclaredTool:
    """一个 YAML 装出来的工具：规格 + 处理函数 + 来自哪个文件。"""

    name: str
    spec: ToolSpec
    handler: Callable[..., ToolResult]
    source: str
    #: YAML 里写了 ``enabled: true``（人的授权写在文件里，不写在代码里）
    enabled_in_file: bool = False


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_http_get(url: str, *, timeout_s: float = DEFAULT_TIMEOUT_S,
                      headers: Mapping[str, str] | None = None) -> HttpReply:
    try:
        rep = egress.get(url, headers=dict(headers or {}),
                         timeout_ms=duration.to_ms(f"{timeout_s}s"),
                         raise_for_status=False)
    except egress.EgressError as exc:
        warn("core/tools/declarative.py:106 Exception", err=repr(exc))
        return HttpReply(status=0, error=f"timeout: {type(exc).__name__}")
    if not rep.ok:
        return HttpReply(status=int(rep.status or 0), error=f"http_{rep.status}")
    return HttpReply(status=int(rep.status or 200), text=rep.text)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        import yaml
    except Exception as exc:  # pragma: no cover - 环境缺依赖
        raise DeclarativeToolError("no_yaml", str(exc)) from exc
    try:
        data = yaml.safe_load(io.open(path, encoding="utf-8-sig").read())
    except Exception as exc:  # noqa: BLE001 - 解析错误要带文件名
        raise DeclarativeToolError("bad_yaml", f"{path.name}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise DeclarativeToolError("not_a_mapping", path.name)
    return data


def _parse_effects(raw: Any) -> frozenset[Effect]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise DeclarativeToolError("no_effects", "至少要声明一个 effect")
    out: set[Effect] = set()
    for item in raw:
        name = _as_text(item).lower()
        try:
            eff = Effect(name)
        except ValueError as exc:
            raise DeclarativeToolError("unknown_effect", name) from exc
        if eff not in ALLOWED_EFFECTS:
            raise DeclarativeToolError("forbidden_effect", name)
        out.add(eff)
    return frozenset(out)


def _parse_args(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise DeclarativeToolError("bad_args", "args 必须是映射")
    args = {str(k): str(v) for k, v in raw.items()}
    return args


def _parse_hosts(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise DeclarativeToolError("no_allow_hosts", "allow_hosts 必填")
    hosts: list[str] = []
    for item in raw:
        host = _as_text(item).lower().lstrip(".")
        if not host or "/" in host or "{" in host:
            raise DeclarativeToolError("bad_allow_hosts", str(item))
        hosts.append(host)
    return tuple(hosts)


def _host_allowed(url: str, hosts: tuple[str, ...]) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in hosts)


def _check_url_template(url: str) -> None:
    if not url:
        raise DeclarativeToolError("no_url", "http.url 必填")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise DeclarativeToolError("bad_scheme", parts.scheme)
    if "{" in (parts.hostname or "") or "}" in (parts.hostname or ""):
        raise DeclarativeToolError("dynamic_host", "主机部分不许带占位符")
    if "{" not in url:
        return
    if url.count("{") > MAX_PLACEHOLDERS:
        raise DeclarativeToolError("too_many_placeholders", str(url.count("{")))


def load_tool_file(path: str | Path, *, http_get: Callable[..., HttpReply] | None = None
                   ) -> DeclaredTool:
    """读一个 YAML，装成一个工具。任何不合规都抛 ``DeclarativeToolError``。"""
    p = Path(path)
    data = _load_yaml(p)
    name = _as_text(data.get("name"))
    family = _as_text(data.get("family"))
    returns = _as_text(data.get("returns"))
    if not name:
        raise DeclarativeToolError("no_name", p.name)
    if not family:
        raise DeclarativeToolError("no_family", p.name)
    if not returns:
        raise DeclarativeToolError("no_returns", p.name)

    effects = _parse_effects(data.get("effects"))
    args = _parse_args(data.get("args"))
    hosts = _parse_hosts(data.get("allow_hosts"))

    http = data.get("http") or {}
    if not isinstance(http, Mapping):
        raise DeclarativeToolError("bad_http", "http 必须是映射")
    method = _as_text(http.get("method", "GET")).upper()
    if method not in ALLOWED_METHODS:
        raise DeclarativeToolError("forbidden_method", method)
    url_tpl = _as_text(http.get("url"))
    _check_url_template(url_tpl)
    if not _host_allowed(url_tpl, hosts):
        raise DeclarativeToolError("host_not_allowed", url_tpl)

    limits = dict(data.get("limits") or {})
    timeout_s = float(limits.get("timeout_s", DEFAULT_TIMEOUT_S) or DEFAULT_TIMEOUT_S)
    max_chars = int(limits.get("max_chars", DEFAULT_MAX_CHARS) or DEFAULT_MAX_CHARS)
    # 声明的参数名 → 类型（`ToolSpec.args` 的形状；`check_call` 拿它挡未知参数）
    declared_args = {str(k): str(v) for k, v in args.items()}

    try:
        owner = p.relative_to(_repo_root()).as_posix()
    except Exception:  # noqa: BLE001 - 临时目录里的文件
        warn("core/tools/declarative.py:226 Exception", err=repr(__import__("sys").exc_info()[1]))
        owner = p.as_posix()

    spec = ToolSpec(
        name=name,
        family=family,
        effects=effects,
        args=declared_args,
        returns=returns,
        perms=tuple(str(x) for x in (data.get("perms") or ())),
        limits={"timeout_s": timeout_s, "max_chars": max_chars},
        idempotent=bool(data.get("idempotent", True)),
        owner=f"YAML:{owner}",
        enabled=False,
    )
    ok, reason = spec.validate()
    if not ok:
        raise DeclarativeToolError(reason, p.name)

    getter = http_get or _default_http_get
    order = tuple(args)

    def handler(*, target: str = "", what: str = "",
                params: Mapping[str, Any] | None = None) -> ToolResult:
        payload: dict[str, Any] = dict(params or {})
        values: dict[str, str] = {}
        for idx, key in enumerate(order):
            raw = payload.get(key)
            if raw in (None, "") and idx == 0:
                raw = what
            values[key] = _as_text(raw)
        for key in order:
            if "{" + key + "}" in url_tpl and not values.get(key):
                return ToolResult(status="no_result",
                                  content=f"缺少参数：{key}",
                                  source=owner, at=_now())
        url = url_tpl
        for key, value in values.items():
            url = url.replace("{" + key + "}", urllib.parse.quote(value, safe=""))
        try:
            reply = getter(url, timeout_s=timeout_s, headers={})
        except Exception as exc:  # noqa: BLE001 - 注入的取数器抛错也不许冒泡
            warn("core/tools/declarative.py:267 Exception", err=repr(exc))
            return ToolResult(status="timeout", content=f"timeout: {type(exc).__name__}",
                              source=url, at=_now())
        status = int(getattr(reply, "status", 0) or 0)
        text = str(getattr(reply, "text", "") or "").strip()
        if status == 0:
            return ToolResult(status="timeout",
                              content=str(getattr(reply, "error", "") or "timeout"),
                              source=url, at=_now())
        if status >= 500:
            return ToolResult(status="http_5xx", content=f"HTTP {status}", source=url, at=_now())
        if status >= 400:
            return ToolResult(status="http_4xx", content=f"HTTP {status}", source=url, at=_now())
        if not text:
            return ToolResult(status="no_result", content="", source=url, at=_now())
        truncated = len(text) > max_chars
        return ToolResult(status="ok", content=text[:max_chars], source=url,
                          at=_now(), truncated=truncated)

    return DeclaredTool(name=name, spec=spec, handler=handler, source=owner,
                        enabled_in_file=bool(data.get("enabled", False)))


def _now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_declared_tools(directory: str | Path | None = None, *,
                        http_get: Callable[..., HttpReply] | None = None
                        ) -> tuple[DeclaredTool, ...]:
    """目录里所有 ``*.yaml``。坏文件跳过并 log，不让一个错文件拖垮整批。"""
    root = Path(directory) if directory else _repo_root() / TOOL_DIR
    if not root.is_dir():
        return ()
    out: list[DeclaredTool] = []
    for path in sorted(root.iterdir()):
        if path.suffix.lower() not in SUFFIXES or not path.is_file():
            continue
        if path.name.startswith("_"):
            continue  # `_example_*.yaml` 只是文档，不装载
        try:
            out.append(load_tool_file(path, http_get=http_get))
        except DeclarativeToolError as exc:
            log.warning("declared_tool_rejected file=%s reason=%s detail=%s",
                        path.name, exc.reason, exc.detail)
    return tuple(out)


def bind_declared(registry: ToolRegistry, directory: str | Path | None = None, *,
                  enable: bool = False,
                  http_get: Callable[..., HttpReply] | None = None) -> tuple[str, ...]:
    """把声明式工具登记进注册表。**默认只登记不启用**（与工具包同一口径）。"""
    names: list[str] = []
    for tool in load_declared_tools(directory, http_get=http_get):
        ok, reason = registry.register(tool.spec)
        if not ok:
            log.warning("declared_tool_rejected name=%s reason=%s", tool.name, reason)
            continue
        registry.bind(tool.name, tool.handler)
        names.append(tool.name)
        if enable and _file_enabled(tool):
            registry.enable(tool.name)
    return tuple(names)


def _file_enabled(tool: DeclaredTool) -> bool:
    """YAML 里写着 ``enabled: true`` 才算「人已经授过权」。"""
    return bool(getattr(tool, "enabled_in_file", False))
