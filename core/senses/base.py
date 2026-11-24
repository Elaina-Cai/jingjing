"""感官快照的形状与渲染（纯逻辑，不碰操作系统）。"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 剪贴板进提示词的字数上限：剪贴板可能是几十万字的一份日志。
CLIPBOARD_MAX_CHARS = 1000
#: 窗口标题的显示上限。
TITLE_MAX_CHARS = 80
#: 整块 ``[现在这一屏]`` 的字数上限（判的输入预算是 3600 字，感官不许占超过五分之一）。
SENSES_BLOCK_MAX_CHARS = 800
BLOCK_HEADER = "[现在这一屏]"
BLOCK_FOOTER = "（这是我自己看见的，不是群里说的话）"

#: 感官自己的失败码（与工具包的 ``STATUS_CODES`` 分开：感官不是工具调用）。
SENSE_ERROR_CODES = ("blocked", "unsupported", "failed")


class SensesError(RuntimeError):
    """感官这一侧失败：``code`` 取自 :data:`SENSE_ERROR_CODES`。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "failed")
        self.detail = str(detail or "")
        super().__init__(f"{self.code}: {self.detail}" if self.detail else self.code)


def _clip(text: str, limit: int) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[: max(0, limit - 1)] + "…"


@dataclass(frozen=True)
class WindowInfo:
    """前台窗口：标题、进程名、屏幕矩形、是否真的在前台。"""

    title: str = ""
    exe: str = ""
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    focused: bool = False

    def is_empty(self) -> bool:
        return not self.title and not self.exe


@dataclass(frozen=True)
class ScreenShot:
    """一张截图。``digest`` 是画面内容的指纹（同画面不重复落盘）。"""

    path: str = ""
    width: int = 0
    height: int = 0
    digest: str = ""
    taken_at: str = ""


@dataclass(frozen=True)
class SensorySnapshot:
    """这一屏的全套观察。任何一个字段都可以缺失——读不到就是读不到。"""

    window: WindowInfo = field(default_factory=WindowInfo)
    screen: ScreenShot = field(default_factory=ScreenShot)
    clipboard: str = ""
    notes: tuple[str, ...] = ()


def render_snapshot(
    snapshot: SensorySnapshot | None, *, max_chars: int = SENSES_BLOCK_MAX_CHARS
) -> str:
    """排成一小块文字。**空快照返回空串**（不许注入只有抬头的空块）。"""
    if snapshot is None:
        return ""
    lines: list[str] = []
    win = snapshot.window
    if not win.is_empty():
        who = win.exe or "未知进程"
        title = _clip(win.title, TITLE_MAX_CHARS) or "（无标题）"
        lines.append(f"- 前台：{who} · {title}")
    shot = snapshot.screen
    if shot.path:
        res = f"{shot.width}×{shot.height}" if shot.width and shot.height else "?"
        lines.append(f"- 屏幕：{res} 图 {shot.digest[:8]}（{shot.taken_at}）")
    clip = _clip(" ".join(str(snapshot.clipboard or "").split()), CLIPBOARD_MAX_CHARS)
    if clip:
        lines.append(f"- 剪贴板：{clip}")
    for note in snapshot.notes or ():
        text = _clip(" ".join(str(note).split()), 120)
        if text:
            lines.append(f"- 注：{text}")
    if not lines:
        return ""
    budget = max(0, int(max_chars)) - len(BLOCK_HEADER) - len(BLOCK_FOOTER) - 2
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget:
            continue
        kept.append(line)
        used += len(line) + 1
    if not kept:
        return ""
    return "\n".join((BLOCK_HEADER, *kept, BLOCK_FOOTER))
