# -*- coding: utf-8 -*-
"""读本地文档（只读）。

为什么需要它：2026-11-18 的 C 实验里，用户让她「找项目里那份部署说明」，她只能回
「我翻不了项目里的文件」——工具表里没有任何能读磁盘的手。经验教会她「先查索引」，
却没有手去查。

边界（这是这个工具唯一值得争的东西）：

* **只读**，`effects=read`，不写、不改、不发；
* 只允许仓库根下的 `docs/` 与 `output/`，以及根目录的 `*.md`——项目文档与产出，
  不是整台机器；
* 拒 `.env*`、`data/`、`backups/`、`.git`、虚拟环境、`*.db`/`*.key`/`*.pem`；
* 绝对路径、`..` 逃逸、符号链接出界一律拒；
* 单份 ≤256 KB，回给模型的正文截到 8000 字，并明说「被截断」。

要放宽（比如让她读源码）就改 `ROOTS`，别在这里打补丁。
"""
from __future__ import annotations

from pathlib import Path

import structlog

from agents.tools.base import BaseTool, ToolResult

log = structlog.get_logger()

DEFAULT_ROOT = Path(__file__).resolve().parents[2]
ROOTS = ("docs", "output")
TOP_LEVEL_SUFFIXES = (".md",)
MAX_BYTES = 256 * 1024
MAX_CHARS = 8000
DENY_PARTS = {".git", ".venv", ".venv_iso", ".venv_it", "data", "backups",
              "__pycache__", "node_modules", ".workbuddy"}
DENY_PREFIX = (".env",)
DENY_SUFFIX = (".db", ".sqlite", ".key", ".pem", ".p12", ".pfx")


class ReadFileTool(BaseTool):
    name = "read_file"
    description = (
        "读项目里的一份文档或产出（只读）。路径写**相对仓库根**的相对路径，"
        "例如 docs/README.md、output/×××.md。只能读 docs/、output/ 和根目录的 *.md；"
        "读不到就直说读不到，不要凭印象补内容。"
    )
    parameters = {
        "path": {"type": "string", "description": "相对仓库根的路径，如 docs/README.md"},
        "max_chars": {"type": "integer", "description": "最多回多少字（默认 8000，上限 8000）"},
    }

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root).resolve() if root else DEFAULT_ROOT

    # ---------------------------------------------------------------- 路径闸

    def _resolve(self, raw: str) -> tuple[Path | None, str]:
        """返回 (绝对路径, 错误)。错误为空串表示通过。"""
        text = str(raw or "").strip().replace("\\", "/")
        if not text:
            return None, "要给路径，例如 docs/README.md"
        candidate = Path(text)
        if candidate.is_absolute() or text.startswith("/") or ":" in text.split("/")[0]:
            return None, "只要相对路径：绝对路径不给读"
        if any(part == ".." for part in candidate.parts):
            return None, "路径里不许有 .."
        relative = Path(*[p for p in candidate.parts if p not in ("", ".")])
        if not relative.parts:
            return None, "要给文件，不是目录"
        denied_parts = {part.casefold() for part in DENY_PARTS}
        if any(part.casefold() in denied_parts for part in relative.parts):
            return None, "这个目录不给读：%s" % "/".join(
                p for p in relative.parts if p.casefold() in denied_parts
            )
        name = relative.name.lower()
        if name.startswith(DENY_PREFIX):
            return None, "这类文件不给读：%s" % relative.name
        if name.endswith(DENY_SUFFIX):
            return None, "这类文件不给读：%s" % relative.name
        allowed = (len(relative.parts) > 1 and relative.parts[0] in ROOTS) or (
            len(relative.parts) == 1 and relative.suffix.lower() in TOP_LEVEL_SUFFIXES)
        if not allowed:
            return None, "只能读 %s 下的文件，或根目录的 %s" % (
                " / ".join(ROOTS), " ".join(TOP_LEVEL_SUFFIXES))
        target = (self.root / relative).resolve()
        try:
            target.relative_to(self.root)
        except ValueError:
            return None, "路径跑到仓库外面了"
        if not target.is_file():
            return None, "没有这个文件：%s" % relative.as_posix()
        return target, ""

    # ---------------------------------------------------------------- 执行

    async def execute(self, path: str = "", max_chars: int = MAX_CHARS, **_: object) -> ToolResult:
        target, why = self._resolve(path)
        if target is None:
            return ToolResult("", error=why)
        try:
            size = target.stat().st_size
        except OSError as exc:
            return ToolResult("", error="读不了：%s" % repr(exc)[:80])
        if size > MAX_BYTES:
            return ToolResult("", error="这份太大了（%.0f KB，上限 %d KB），换个更具体的文件"
                              % (size / 1024.0, MAX_BYTES // 1024))
        try:
            raw = target.read_bytes()
        except OSError as exc:
            return ToolResult("", error="读不了：%s" % repr(exc)[:80])
        if b"\x00" in raw[:4096]:
            return ToolResult("", error="这看着是二进制文件，不是文档，我不读")
        body = raw.decode("utf-8", errors="replace")
        cap = max(200, min(int(max_chars or MAX_CHARS), MAX_CHARS))
        cut = len(body) > cap
        shown = body[:cap]
        relative = target.relative_to(self.root).as_posix()
        head = "「%s」%d 字%s" % (relative, len(body), "（只给前 %d 字）" % cap if cut else "")
        log.info("read_file ok path=%s chars=%d cut=%s", relative, len(body), cut)
        return ToolResult(head + "\n" + shown,
                          data={"path": relative, "chars": len(body), "truncated": cut})
