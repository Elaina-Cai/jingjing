"""MCP 工具源（`core/execution/mcp.py`）的验收。

跑法：python -X utf8 -u tests/test_mcp_tools.py
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# 临时目录的唯一入口（见 tests/_tempdirs.py）：不给 dir= 时系统 TEMP 不可写会静默回落到仓库根。
from _tempdirs import temp_base  # noqa: E402

from core.execution import mcp as M  # noqa: E402
from core.tools.registry import STATUS_CODES, Effect, ToolRegistry  # noqa: E402

SERVER_SCRIPT = str(ROOT / "tests" / "fixtures" / "fake_mcp_server.py")
_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    if cond:
        print(f"PASS {name}")
        return
    _FAILED += 1
    print(f"FAIL {name} {detail}")


class FakeTransport:
    """无进程的传输层：按脚本回包，记录发出去的东西。"""

    def __init__(self, *, tools=None, call_result=None, crash=False, timeout=False):
        self.sent: list[dict] = []
        self.closed = False
        self._tools = tools if tools is not None else [
            {"name": "echo", "description": "回显",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
            {"name": "rm_rf", "description": "危险", "inputSchema": {}},
        ]
        self._call_result = call_result
        self._crash = crash
        self._timeout = timeout

    def request(self, payload, *, timeout_s):
        self.sent.append(dict(payload))
        if self._timeout:
            raise M.McpError("timeout", "假的超时")
        if self._crash:
            raise M.McpError("server_exited", "1")
        method = payload.get("method")
        mid = payload.get("id")
        if mid is None:
            return {}
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"protocolVersion": M.MCP_PROTOCOL_VERSION,
                               "serverInfo": {"name": "fake"}}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": self._tools}}
        if method == "tools/call":
            params = payload.get("params") or {}
            if params.get("name") == "raise_jsonrpc":
                return {"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32601, "message": "no such tool"}}
            return {"jsonrpc": "2.0", "id": mid, "result": self._call_result or {
                "content": [{"type": "text", "text": "回显内容"}], "isError": False}}
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": method}}

    def close(self):
        self.closed = True


def spec(**kw) -> M.McpServerSpec:
    base = dict(name="fake", command=(sys.executable, SERVER_SCRIPT))
    base.update(kw)
    return M.McpServerSpec(**base)


def main() -> int:
    # ---------- 1. 有界常量 ----------
    check("协议版本与自我介绍齐", M.MCP_PROTOCOL_VERSION and M.CLIENT_NAME and M.CLIENT_VERSION)
    check("服务器 ≤3 台", M.MAX_SERVERS == 3)
    check("每台工具 ≤20 个", M.MAX_TOOLS == 20)
    check("每次请求 ≤8 秒", M.DEFAULT_TIMEOUT_S == 8.0)
    check("正文 ≤4000 字", M.MAX_CONTENT_CHARS == 4000)
    check("MCP 工具只有 read", M.FORCED_EFFECTS == frozenset({Effect.READ}), str(M.FORCED_EFFECTS))

    # ---------- 2. 真进程：握手 / 列工具 / 调工具 ----------
    client = M.McpClient(M.StdioTransport([sys.executable, SERVER_SCRIPT]), server="fake")
    try:
        tools = client.list_tools()
        names = [t.name for t in tools]
        check("真进程能列出工具", "echo" in names and "boom" in names, str(names))
        check("工具带 inputSchema", any(dict(t.input_schema) for t in tools))

        specs = M.tool_specs(client, spec())
        check("工具名统一加 mcp_ 前缀", all(s.name.startswith("mcp_") for s in specs),
              str([s.name for s in specs]))
        check("effects 强制 read", all(s.effects == M.FORCED_EFFECTS for s in specs))
        check("登记但一律 disabled", all(s.enabled is False for s in specs))
        check("owner 标着来自哪台服务器", all(s.owner == "MCP:fake" for s in specs),
              str([s.owner for s in specs]))
        check("参数从 inputSchema 抄过来",
              any(dict(s.args) == {"text": "string"} for s in specs),
              str([dict(s.args) for s in specs]))

        ok = M.make_handler(client, "echo", server="fake")
        res = ok(what="你好世界")
        check("调工具 → ok 且带回正文", res.status == "ok" and "你好世界" in res.content,
              f"{res.status} {res.content}")

        empty = M.make_handler(client, "nothing", server="fake")()
        check("空内容 → no_result（不是 ok）", empty.status == "no_result", empty.status)

        boom = M.make_handler(client, "boom", server="fake")()
        check("isError → failed（不是 ok）", boom.status == "failed", boom.status)
        check("failed 是登记过的状态码", boom.status in STATUS_CODES)

        long_text = M.make_handler(client, "echo", server="fake", max_chars=5)(what="一二三四五六七八")
        check("超长截断并标 truncated", long_text.truncated and len(long_text.content) <= 5,
              f"{long_text.content!r} {long_text.truncated}")
    finally:
        client.close()

    # ---------- 3. 白名单 / 黑名单 ----------
    c2 = M.McpClient(FakeTransport(), server="fake")
    deny = M.tool_specs(c2, spec(deny_tools=("rm_*",)))
    check("deny_tools 优先", [s.name for s in deny] == ["mcp_echo"], str([s.name for s in deny]))
    allow = M.tool_specs(c2, spec(allow_tools=("rm_*",)))
    check("allow_tools 只放匹配的", [s.name for s in allow] == ["mcp_rm_rf"],
          str([s.name for s in allow]))
    over = M.tool_specs(c2, spec(), max_tools=1)
    check("工具数有上限", len(over) == 1, str(len(over)))

    # ---------- 4. 失败分类 ----------
    t_timeout = FakeTransport(timeout=True)
    c3 = M.McpClient(t_timeout, timeout_s=0.1)
    try:
        c3.list_tools()
        check("超时要抛 McpError", False)
    except M.McpError as exc:
        check("超时要抛 McpError", exc.code == "timeout", exc.code)

    handler_timeout = M.make_handler(c3, "echo", server="fake")()
    check("超时在 handler 里 → status=timeout（查不了）", handler_timeout.status == "timeout",
          handler_timeout.status)

    c4 = M.McpClient(FakeTransport(crash=True), server="fake")
    handler_crash = M.make_handler(c4, "echo", server="fake")()
    check("进程死 → status=timeout", handler_crash.status == "timeout", handler_crash.status)

    t_err = FakeTransport()
    c5 = M.McpClient(t_err, server="fake")
    bad = M.make_handler(c5, "raise_jsonrpc", server="fake")()
    check("JSON-RPC error → failed", bad.status == "failed", bad.status)

    # ---------- 5. 服务器配置 ----------

    with tempfile.TemporaryDirectory(prefix="mcp_", dir=str(temp_base())) as tmp:
        root = Path(tmp)
        (root / "ok.yaml").write_text(
            "name: demo\ncommand: [python, x.py]\ntimeout_s: 3\nenabled: true\n"
            "allow_tools: ['read_*']\ndeny_tools: ['*secret*']\n", encoding="utf-8")
        (root / "bad.yaml").write_text("name: nope\n", encoding="utf-8")
        s_ok = M.load_server_spec(root / "ok.yaml")
        check("服务器配置读得出来", s_ok.name == "demo" and s_ok.timeout_s == 3.0, str(s_ok))
        check("文件里的 enabled 单独记住", s_ok.enabled_in_file is True)
        check("白名单读成元组", s_ok.allow_tools == ("read_*",) and s_ok.deny_tools == ("*secret*",),
              f"{s_ok.allow_tools} {s_ok.deny_tools}")
        try:
            M.load_server_spec(root / "bad.yaml")
            check("没 command 要抛", False)
        except M.McpError as exc:
            check("没 command 要抛", exc.code == "no_command", exc.code)
        loaded = M.load_servers(tmp)
        check("坏文件被跳过，好文件还在", [s.name for s in loaded] == ["demo"],
              str([s.name for s in loaded]))

    # ---------- 6. 装进注册表 ----------
    reg = ToolRegistry()
    t = FakeTransport()
    factory = lambda _spec: M.McpClient(t, server=_spec.name)  # noqa: E731
    names = M.bind_servers(reg, directory=None or str(ROOT / "configs" / "mcp"),
                          enable=False, client_factory=factory)
    check("bind_servers 返回装上的工具名", isinstance(names, list))
    if names:
        first = reg.get(names[0])
        check("登记后的工具默认 disabled", first is not None and first.enabled is False)
        check("工具只读", first.effects == M.FORCED_EFFECTS)

    reg2 = ToolRegistry()
    names2 = M.bind_servers(reg2, directory=str(ROOT / "configs" / "mcp"),
                            enable=True, client_factory=factory)
    check("文件 enabled: false 时 enable=True 也不启用（模板就是 false）",
          all(reg2.get(n) is not None and reg2.get(n).enabled is False for n in names2),
          str([(n, reg2.get(n).enabled) for n in names2]))

    with tempfile.TemporaryDirectory(prefix="mcp_on_", dir=str(temp_base())) as tmp_on:
        Path(tmp_on, "on.yaml").write_text(
            "name: on\ncommand: [python, x.py]\nenabled: true\n", encoding="utf-8")
        reg3 = ToolRegistry()
        names3 = M.bind_servers(reg3, directory=tmp_on, enable=True, client_factory=factory)
        check("文件 enabled: true 且 enable=True 才启用",
              bool(names3) and all(reg3.get(n).enabled for n in names3),
              str([(n, reg3.get(n).enabled) for n in names3]))
        reg4 = ToolRegistry()
        names4 = M.bind_servers(reg4, directory=tmp_on, enable=False, client_factory=factory)
        check("文件 enabled: true 但调用方 enable=False 仍不启用",
              bool(names4) and all(reg4.get(n).enabled is False for n in names4),
              str([(n, reg4.get(n).enabled) for n in names4]))

    # ---------- 7. 静态纪律 ----------
    src = io.open(ROOT / "core" / "execution" / "mcp.py", encoding="utf-8").read()
    check("不许 shell=True", "shell=True" not in src)
    check("不许 os.system / Popen(shell 字符串)", "os.system" not in src)
    check("stderr 丢掉（防管道卡死）", "stderr=subprocess.DEVNULL" in src)
    check("不 import speech / routing / agents",
          "core.speech" not in src and "core.routing" not in src and "agents." not in src)
    check("不直接投递（不 import delivery 出口）",
          "core.speech.delivery" not in src and "send_text" not in src)

    print(f"RESULT {_FAILED} failed")
    return _FAILED


if __name__ == "__main__":
    raise SystemExit(main())
