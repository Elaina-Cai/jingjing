# -*- coding: utf-8 -*-
"""记忆索引的取值工具：`read_memory`（2026-10-23 主人拍板「记忆 push 留索引」）。

提示词里的 ``[事件记忆·索引]`` 只给 key（m1、m2…）、时间和开头一句；正文存在绑定当前
身份的索引快照里。要看细节就调这个工具按 key 取——一次取一条也行，
一次取几条也行，取多了会被截断（``MAX_CHARS_PER_CALL``），免得一条记忆把上下文撑满。

为什么不在这里再查一次库
========================
索引与正文必须来自**同一次** recall，否则「提示词里写着 m1 是加班，取回来变成别的」这种
错位没法排查。所以正文随索引一起落在 extra 里，本工具只做查表与拼行。

与既有记忆工具的分工（``agents/tools/memory.py``）
===================================================
- ``memory_recall``：按**问题**现查（「我缺什么就写什么」），自己决定查什么；
- ``recall_memories``：翻旧账（「还记得…吗」），带 since/before；
- ``remember``：写一条新记忆；
- ``read_memory``（本模块）：读**这一轮已经被推给她的**那几条的全文——key 是提示词里
  看得见的东西，不猜、不重查。
"""
from __future__ import annotations

import re
from typing import Any

from agents.tools.base import BaseTool, ToolResult

__all__ = ["ReadMemoryTool"]

MAX_CHARS_PER_CALL = 10 ** 9   # 2026-10-31 取消上限（原来 4000 字就截断）
_SPLIT_RE = re.compile(r"[,，、;；\s]+")


class ReadMemoryTool(BaseTool):
    name = "read_memory"
    description = (
        "读某条记忆的全文。提示词里的 [事件记忆·索引] 只给 key（m1、m2…）和开头一句，"
        "要引用细节（谁说的、什么时候、原话）就按 key 取一次。"
        "key 只能用这一轮索引里出现过的；写错会告诉你这一轮有哪些。"
    )
    parameters = {
        "keys": {
            "type": "string",
            "description": "索引里的 key，多个用逗号或空格分隔，例如 \"m1,m3\"",
        },
    }

    async def execute(self, **kwargs) -> ToolResult:
        raw = str(kwargs.get("keys") or kwargs.get("key") or "").strip()
        from core.memory.index_scope import bound_index
        snapshot = bound_index(self.ctx)
        if snapshot is None:
            return ToolResult(output="这一轮没有可用于当前会话身份的记忆索引。", error="no_memory_index")
        entries = snapshot.entries_for(self.ctx)
        if not entries:
            return ToolResult(output="这一轮没有记忆索引。", error="no_memory_index")
        by_key: dict[str, dict[str, Any]] = {}
        by_id: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            by_key[str(entry.get("key") or "")] = entry
            if str(entry.get("memory_id") or ""):
                by_id[str(entry["memory_id"])] = entry
        available = "、".join(key for key in by_key if key)
        wanted = [token for token in _SPLIT_RE.split(raw) if token]
        if not wanted:
            return ToolResult(
                output="要取哪一条？这一轮可用的 key：%s" % available,
                error="memory_key_required",
            )
        lines: list[str] = []
        missing: list[str] = []
        picked: list[dict[str, Any]] = []
        used = 0
        for token in wanted:
            entry = by_key.get(token) or by_id.get(token)
            if entry is None:
                missing.append(token)
                continue
            from core.terminal.memory_gateway import _stamp_of

            stamp = _stamp_of(str(entry.get("occurred_at") or ""))
            who = str(entry.get("speaker") or "")
            head = " ".join(
                part
                for part in (
                    str(entry.get("key") or ""),
                    str(entry.get("source_label") or ""),
                    ("[%s]" % stamp) if stamp else "",
                    (who + ":") if who else "",
                )
                if part
            )
            body = str(entry.get("content") or "").strip()
            line = ("%s %s" % (head, body)).strip()
            if used + len(line) > MAX_CHARS_PER_CALL:
                lines.append("…（再取就超了本次上限 %d 字，剩下的下次再取）" % MAX_CHARS_PER_CALL)
                break
            lines.append(line)
            used += len(line) + 1
            picked.append(entry)
        if not lines:
            return ToolResult(
                output="索引里没有 %s。这一轮可用的 key：%s" % ("、".join(missing), available),
                error="unknown_memory_key",
            )
        if missing:
            lines.append("（没找到 %s；这一轮可用的 key：%s）" % ("、".join(missing), available))
        return ToolResult(
            output="\n".join(lines),
            data={
                "keys": [str(entry.get("key") or "") for entry in picked],
                "memory_ids": [str(entry.get("memory_id") or "") for entry in picked],
                "source_event_ids": [
                    event_id
                    for entry in picked
                    for event_id in (entry.get("source_event_ids") or ())
                ],
                "memory_claims": [
                    {
                        "content": str(entry.get("content") or ""),
                        "state": (
                            "unknown"
                            if "依据未核实" in str(entry.get("source_label") or "")
                            else "unverified"
                            if "没有可核实来源" in str(entry.get("source_label") or "")
                            else ""
                        ),
                        "source_event_ids": list(entry.get("source_event_ids") or ()),
                        "metadata": {
                            **(entry.get("metadata") or {}),
                            "conflict": (
                                bool((entry.get("metadata") or {}).get("conflict"))
                                or "冲突待核对" in str(entry.get("source_label") or "")
                            ),
                        },
                    }
                    for entry in picked
                ],
            },
        )
