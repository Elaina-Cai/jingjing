"""测试用假 MCP 服务器：每行一条 JSON-RPC 2.0（stdio 传输）。

用法：
    python tests/fixtures/fake_mcp_server.py            # 正常
    python tests/fixtures/fake_mcp_server.py --slow     # 每个请求睡 2 秒（测超时）
    python tests/fixtures/fake_mcp_server.py --crash    # 收到 initialize 就退出（测进程死）
"""

from __future__ import annotations

import json
import sys
import time

# 管道里 stdout 会退回系统代码页，这里钉死 UTF-8（与 core/execution/mcp.py 对齐）
try:
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
except Exception:  # noqa: BLE001
    pass

SLOW = "--slow" in sys.argv
CRASH = "--crash" in sys.argv

TOOLS = [
    {
        "name": "echo",
        "description": "把 text 原样返回",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
    {
        "name": "nothing",
        "description": "永远返回空内容",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "boom",
        "description": "永远报错",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "rm_rf",
        "description": "假装是个危险工具（用 deny 白名单挡掉）",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        if SLOW:
            time.sleep(2.0)
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        if CRASH and method == "initialize":
            return 3
        if mid is None:  # 通知，不回
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0.1"},
            }})
            continue
        if method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
            continue
        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                body = str(args.get("text") or args.get("query") or "")
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": body}], "isError": False}})
            elif name == "nothing":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": ""}], "isError": False}})
            elif name == "boom":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "炸了"}], "isError": True}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {
                    "code": -32601, "message": f"unknown tool {name}"}})
            continue
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": method}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
