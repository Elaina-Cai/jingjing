"""MCP（Model Context Protocol）客户端：把外部 MCP 服务器当成**只读工具源**。

为什么是「只读」
----------------
MCP 服务器的能力是**外部进程**自己声明的，我们既跑不了它的代码审查、也拦不住它内部去发请求。
所以这里只做一件事：把它声明的工具登记成 ``effects={read}`` 的 ``ToolSpec``——
写动作与外部发送永远不经过 MCP，只能走出口注册表与 ``core/execution/gui/``。

六条纪律（每条都有对应的测例）
------------------------------
1. **不许 shell**：``StdioTransport`` 用 ``shell=False`` + 参数数组；命令里出现 ``&&`` / ``|`` 不会被解释。
2. **服务器与被调工具都要人开**：``configs/mcp/*.yaml`` 里 ``enabled: true`` **并且**
   调用点 ``bind_servers(enable=True)``；工具登记后仍是 ``enabled=False``，要 ``registry.enable()``。
3. **有界**：服务器 ≤ ``MAX_SERVERS``（3），每台工具 ≤ ``MAX_TOOLS``（20），
   每次请求 ≤ ``DEFAULT_TIMEOUT_S``（8 秒），返回正文 ≤ ``MAX_CONTENT_CHARS``（4000 字）。
4. **白名单**：``allow_tools`` / ``deny_tools`` 都是 fnmatch 模式；``deny_tools`` 永远优先。
5. **失败分类不许混**：进程起不来 / 协议错 → ``McpError``（调用方当「查不了」）；
   对方 ``isError: true`` → ``status="failed"``（调用了，对方说不行）；空内容 → ``no_result``。
6. **stderr 丢掉**：``stderr=DEVNULL``，否则服务器写满管道会把它自己卡死。

协议形状（stdio 传输）：**每行一条 JSON-RPC 2.0 消息**，不是 Content-Length 头。
"""

from __future__ import annotations

import fnmatch
import json
import os
import queue
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from core.tools.registry import Effect, ToolResult, ToolSpec
from safelog import warn

import structlog

log = structlog.get_logger(__name__)

#: 服务器目录（一个 yaml = 一台服务器）
SERVER_DIR = "configs/mcp"

#: 协议版本与自我介绍（``initialize`` 要这两个）
MCP_PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "yinyue"
CLIENT_VERSION = "0.1"

#: 有界
DEFAULT_TIMEOUT_S = 8.0
MAX_SERVERS = 3
MAX_TOOLS = 20
MAX_CONTENT_CHARS = 4000

#: MCP 工具一律只有这一个 effect
FORCED_EFFECTS = frozenset({Effect.READ})


class McpError(RuntimeError):
    """协议/进程层面的失败（起不来、超时、返回不是 JSON-RPC）。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class McpTool:
    """服务器声明的一个工具（``tools/list`` 的一项）。"""

    name: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)


class Transport(Protocol):
    """一句话：把一条 JSON-RPC 发出去、把对应该 id 的那条收回来。"""

    def request(self, payload: Mapping[str, Any], *, timeout_s: float
                ) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


class StdioTransport:
    """子进程 + 每行一条 JSON-RPC。``shell=False``。"""

    def __init__(self, command: Sequence[str], *, env: Mapping[str, str] | None = None,
                 cwd: str | None = None) -> None:
        argv = [str(x) for x in command if str(x)]
        if not argv:
            raise McpError("no_command", "")
        child_env = dict(os.environ)
        # 子进程的 stdout 在管道里会退回系统代码页（Windows 上是 cp936），中文 JSON 直接炸；
        # 所以两边都钉死 UTF-8：这里给子进程的开关，读取端用 errors="replace" 兜底。
        child_env.setdefault("PYTHONIOENCODING", "utf-8")
        child_env.setdefault("PYTHONUTF8", "1")
        child_env.update({str(k): str(v) for k, v in (env or {}).items()})
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - 命令来自人写的 yaml，且 shell=False
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                env=child_env,
                cwd=cwd,
            )
        except OSError as exc:
            raise McpError("spawn_failed", f"{type(exc).__name__}: {exc}") from exc
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._pump, name="mcp-reader", daemon=True)
        self._reader.start()
        self._lock = threading.Lock()

    def _pump(self) -> None:
        try:
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                self._lines.put(line)
        finally:
            self._lines.put(None)

    def request(self, payload: Mapping[str, Any], *, timeout_s: float
                ) -> Mapping[str, Any]:
        body = json.dumps(dict(payload), ensure_ascii=False)
        with self._lock:
            if self._proc.poll() is not None:
                raise McpError("server_exited", str(self._proc.returncode))
            assert self._proc.stdin is not None
            try:
                self._proc.stdin.write(body + "\n")
                self._proc.stdin.flush()
            except OSError as exc:
                raise McpError("write_failed", f"{type(exc).__name__}: {exc}") from exc
        want = payload.get("id")
        deadline_s = float(timeout_s)
        while True:
            try:
                line = self._lines.get(timeout=deadline_s)
            except queue.Empty:
                raise McpError("timeout", f"{deadline_s}s 内没有回包") from None
            if line is None:
                raise McpError("server_exited", str(self._proc.poll()))
            text = line.strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError as exc:
                raise McpError("bad_json", str(exc)) from exc
            if not isinstance(msg, Mapping):
                raise McpError("bad_message", type(msg).__name__)
            if want is None or msg.get("id") == want:
                return msg
            # 别的 id（服务器主动通知或上一轮残留）就丢掉继续等

    def close(self) -> None:
        proc = self._proc
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            warn("core/execution/mcp.py:165 OSError", err=repr(__import__("sys").exc_info()[1]))
        try:
            proc.terminate()
        except OSError:
            warn("core/execution/mcp.py:169 OSError", err=repr(__import__("sys").exc_info()[1]))
        try:
            proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001 - 收尾不许抛
            warn("core/execution/mcp.py:173 Exception", err=repr(__import__("sys").exc_info()[1]))
            try:
                proc.kill()
            except OSError:
                warn("core/execution/mcp.py:176 OSError", err=repr(__import__("sys").exc_info()[1]))


class McpClient:
    """握手 + 列工具 + 调工具。一个 client 对一台服务器。"""

    def __init__(self, transport: Transport, *, timeout_s: float = DEFAULT_TIMEOUT_S,
                 server: str = "") -> None:
        self._transport = transport
        self._timeout_s = float(timeout_s)
        self._next_id = 0
        self.server = server
        self._ready = False

    # ---- 底层 ----
    def _call(self, method: str, params: Mapping[str, Any] | None = None,
              *, timeout_s: float | None = None) -> Mapping[str, Any]:
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": dict(params or {}),
        }
        reply = self._transport.request(
            payload, timeout_s=float(timeout_s or self._timeout_s)
        )
        if "error" in reply:
            err = reply.get("error") or {}
            raise McpError("protocol_error", str(err.get("message") or err))
        result = reply.get("result")
        if not isinstance(result, Mapping):
            raise McpError("bad_result", type(result).__name__)
        return result

    def _notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": dict(params or {}),
        }
        # 通知没有 id，回包也不会有；发出去就当成功（StdioTransport 会等 want=None 的第一条）
        try:
            self._transport.request(payload, timeout_s=0.2)
        except McpError:
            warn("core/execution/mcp.py:222 McpError", err=repr(__import__("sys").exc_info()[1]))

    # ---- 协议 ----
    def initialize(self) -> Mapping[str, Any]:
        if self._ready:
            return {"ok": True}
        result = self._call(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        self._notify("notifications/initialized")
        self._ready = True
        return result

    def list_tools(self) -> tuple[McpTool, ...]:
        self.initialize()
        result = self._call("tools/list")
        items = result.get("tools")
        if not isinstance(items, list):
            return ()
        out: list[McpTool] = []
        for raw in items[:MAX_TOOLS]:
            if not isinstance(raw, Mapping):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            schema = raw.get("inputSchema")
            out.append(
                McpTool(
                    name=name,
                    description=str(raw.get("description") or ""),
                    input_schema=schema if isinstance(schema, Mapping) else {},
                )
            )
        return tuple(out)

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None
                  ) -> Mapping[str, Any]:
        self.initialize()
        return self._call("tools/call", {"name": name, "arguments": dict(arguments or {})})

    def close(self) -> None:
        self._transport.close()


# ---------------------------------------------------------------- 服务器配置


@dataclass(frozen=True)
class McpServerSpec:
    """``configs/mcp/<name>.yaml`` 读出来的东西。"""

    name: str
    command: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    allow_tools: tuple[str, ...] = ()
    deny_tools: tuple[str, ...] = ()
    timeout_s: float = DEFAULT_TIMEOUT_S
    enabled_in_file: bool = False


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(x) for x in value if str(x))
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    return ()


def load_server_spec(path: str | Path) -> McpServerSpec:
    import yaml

    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 坏 yaml 报成 McpError
        raise McpError("bad_yaml", f"{p.name}: {type(exc).__name__}") from exc
    if not isinstance(data, Mapping):
        raise McpError("bad_yaml", p.name)
    name = str(data.get("name") or p.stem).strip()
    command = _as_str_tuple(data.get("command"))
    if not command:
        raise McpError("no_command", p.name)
    env = data.get("env") or {}
    return McpServerSpec(
        name=name,
        command=command,
        env={str(k): str(v) for k, v in env.items()} if isinstance(env, Mapping) else {},
        allow_tools=_as_str_tuple(data.get("allow_tools")),
        deny_tools=_as_str_tuple(data.get("deny_tools")),
        timeout_s=float(data.get("timeout_s") or DEFAULT_TIMEOUT_S),
        enabled_in_file=bool(data.get("enabled", False)),
    )


def load_servers(directory: str | Path = SERVER_DIR) -> tuple[McpServerSpec, ...]:
    root = Path(directory)
    if not root.exists():
        return ()
    out: list[McpServerSpec] = []
    for path in sorted(root.glob("*.yaml"))[:MAX_SERVERS]:
        try:
            out.append(load_server_spec(path))
        except McpError as exc:
            log.warning("mcp_server_rejected", file=path.name, reason=exc.code, detail=exc.detail)
    return tuple(out)


# ---------------------------------------------------------------- 装成工具


def _allowed(spec: McpServerSpec, tool_name: str) -> bool:
    if any(fnmatch.fnmatch(tool_name, pat) for pat in spec.deny_tools):
        return False
    if not spec.allow_tools:
        return True
    return any(fnmatch.fnmatch(tool_name, pat) for pat in spec.allow_tools)


def _args_of(tool: McpTool) -> dict[str, str]:
    props = tool.input_schema.get("properties") if isinstance(tool.input_schema, Mapping) else None
    if not isinstance(props, Mapping):
        return {}
    return {str(k): str((v or {}).get("type", "str")) if isinstance(v, Mapping) else "str"
            for k, v in props.items()}


def tool_specs(client: McpClient, spec: McpServerSpec, *,
               max_tools: int = MAX_TOOLS) -> list[ToolSpec]:
    """服务器声明的工具 → ``ToolSpec``（effects 强制 read，enabled 一律 False）。"""
    out: list[ToolSpec] = []
    for tool in client.list_tools():
        if not _allowed(spec, tool.name):
            continue
        name = tool.name if tool.name.startswith("mcp_") else f"mcp_{tool.name}"
        head = (tool.description or tool.name).strip().splitlines()[0][:120]
        out.append(
            ToolSpec(
                name=name,
                family="mcp",
                effects=FORCED_EFFECTS,
                args=_args_of(tool),
                returns=head or "（服务器没写描述）",
                owner=f"MCP:{spec.name}",
                limits={"timeout_s": spec.timeout_s, "max_chars": MAX_CONTENT_CHARS},
                idempotent=True,
                enabled=False,
            )
        )
        if len(out) >= max_tools:
            break
    return out


def _text_of(result: Mapping[str, Any]) -> str:
    content = result.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, Mapping) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
    if not parts and isinstance(result.get("structuredContent"), Mapping):
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return "\n".join(p for p in parts if p).strip()


def make_handler(client: McpClient, tool_name: str, *, server: str = "",
                 max_chars: int = MAX_CONTENT_CHARS) -> Callable[..., ToolResult]:
    """把一次 ``tools/call`` 包成 ``ToolResult``。

    失败三分类：进程/协议错 → ``timeout``（查不了）；``isError: true`` → ``failed``（对方说不行）；
    正文为空 → ``no_result``（调用了，没有东西）。
    """

    def handler(*, target: str = "", what: str = "",
                params: Mapping[str, Any] | None = None) -> ToolResult:
        arguments: dict[str, Any] = {}
        for key, value in dict(params or {}).items():
            arguments[str(key)] = value
        if what and not arguments:
            arguments = {"query": what, "text": what}
        try:
            result = client.call_tool(tool_name, arguments)
        except McpError as exc:
            warn("core/execution/mcp.py:411 McpError", err=repr(exc))
            status = "timeout" if exc.code in ("timeout", "server_exited", "spawn_failed") else "failed"
            return ToolResult(status=status, content=f"{exc.code}: {exc.detail}"[:max_chars],
                             source=f"mcp:{server}", at="")
        text = _text_of(result)
        if result.get("isError"):
            return ToolResult(status="failed", content=text[:max_chars] or "对方报错",
                             source=f"mcp:{server}", at="")
        if not text:
            return ToolResult(status="no_result", content="", source=f"mcp:{server}", at="")
        truncated = len(text) > max_chars
        return ToolResult(status="ok", content=text[:max_chars], source=f"mcp:{server}",
                         at="", truncated=truncated)

    return handler


def bind_servers(registry: Any, *, directory: str | Path = SERVER_DIR,
                 enable: bool = False,
                 client_factory: Callable[[McpServerSpec], McpClient] | None = None
                 ) -> list[str]:
    """把目录里的服务器装进注册表；返回装上的工具名。任何一台坏掉都只跳过它。"""
    names: list[str] = []
    for spec in load_servers(directory):
        client: McpClient | None = None
        try:
            client = (client_factory(spec) if client_factory
                      else McpClient(StdioTransport(spec.command, env=spec.env),
                                     timeout_s=spec.timeout_s, server=spec.name))
            for tool_spec in tool_specs(client, spec):
                ok, reason = registry.register(tool_spec)
                if not ok:
                    log.warning("mcp_tool_rejected", tool=tool_spec.name, reason=reason)
                    continue
                raw_name = tool_spec.name[len("mcp_"):]
                registry.bind(tool_spec.name,
                              make_handler(client, raw_name, server=spec.name))
                if enable and spec.enabled_in_file:
                    registry.enable(tool_spec.name)
                names.append(tool_spec.name)
        except McpError as exc:
            log.warning("mcp_server_failed", server=spec.name, reason=exc.code, detail=exc.detail)
        except Exception as exc:  # noqa: BLE001 - 一台服务器坏掉不许拖垮启动
            log.warning("mcp_server_failed", server=spec.name, reason="unexpected", detail=type(exc).__name__)
    return names
