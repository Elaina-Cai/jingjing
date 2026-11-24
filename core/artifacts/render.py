"""交付物的形状：一段话可以是「一句话 / 一张卡片图 / 一页 md / 一个链接 / 一个文件」。

为什么要有这一层
----------------
微信里发一段 `.md` 的正文，手机上基本没法看；发一张图能看，但图里的字不能再编辑。
所以「交付形态」不是排版偏好，而是决定这条消息有没有用的东西——这也是
``docs/decisions/办_在手的事.md`` 里 ``artifact{kind, renderer, ttl_s}`` 那一槽的实现。

四条纪律
--------
1. **纯函数在前、落盘在后**：``render_card_png`` 只返回字节，``write_card`` 才写盘；
   测试因此不需要临时文件。
2. **有界**：卡片正文 ≤ ``MAX_CARD_CHARS``（1200 字），页 ≤ ``MAX_PAGE_CHARS``（20000 字），
   卡片最多 ``CARD_MAX_PAGES``（3）页；超了截断并**如实标 truncated**，不静默丢。
3. **文件名可预测且安全**：`<时间戳>-<slug>.png`，slug 只留中英文数字，其余换 `-`；
   同名不覆盖（后缀 `-2`）。
4. **不碰出口、不碰判**：这里只产出路径与字节，发不发由出口注册表决定
   （微信适配器今天只自报 `say`，所以文件类会得到 `skipped(reason=unsupported_kind)`——
   那是如实的结果，不是这里的失败）。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core.contracts.delivery import DeliverItem
from safelog import warn

#: 交付形态的封闭集合（``contracts.plan.Artifact.kind`` 的取值域）
KINDS: tuple[str, ...] = ("text", "file", "card", "page", "link")

#: 默认落盘目录（相对仓库根；``data/`` 已在 .gitignore 里）
ARTIFACT_DIR = "data/artifacts"

#: 卡片：宽 420 像素、四周 24 像素留白、行高 30、最多 3 页
CARD_WIDTH = 420
CARD_PAD = 24
CARD_LINE_H = 30
CARD_MAX_PAGES = 3

#: 有界（工具包 §10 的「一次交付不许无限大」）
MAX_CARD_CHARS = 1200
MAX_PAGE_CHARS = 20000

#: 伴随语：一条投递里跟文件一起发的那句话（出口 §5「伴随语 ≤40 字」）
ACCOMPANY_MAX_CHARS = 40

#: 中文字体（Windows 自带；找不到就退到 Pillow 默认字体，仍然能出图）
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
)

_SLUG_KEEP = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


class ArtifactError(ValueError):
    """形状不合法（kind 不认、空正文、字数超界）。"""


def check_kind(kind: str) -> str:
    """校验并归一交付形态；不认的直接抛。"""
    value = str(kind or "").strip().lower()
    if value not in KINDS:
        raise ArtifactError(f"unknown_kind: {kind!r}")
    return value


def slugify(title: str, *, fallback: str = "artifact", limit: int = 40) -> str:
    """标题 → 安全文件名（只留中英文数字，其余换 `-`）。"""
    text = _ILLEGAL.sub("", str(title or "")).strip()
    text = _SLUG_KEEP.sub("-", text).strip("-")
    if not text:
        return fallback
    return text[:limit].strip("-") or fallback


def accompany_text(title: str) -> str:
    """伴随语：只写「这是什么」，≤40 字，不带自己的 @。"""
    head = str(title or "").strip() or "给你做的东西"
    head = " ".join(head.split())
    if len(head) <= ACCOMPANY_MAX_CHARS:
        return head
    return head[: ACCOMPANY_MAX_CHARS - 1] + "…"


def _font(size: int):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001 - 换下一个字体
            warn("core/artifacts/render.py:98 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
    return ImageFont.load_default()


def _wrap(text: str, font, width: int) -> list[str]:
    """按像素宽贪心折行（中文按字、英文按词都不重要，这里逐字折）。"""
    rows: list[str] = []
    for raw in str(text or "").splitlines() or [""]:
        line = ""
        for ch in raw:
            probe = line + ch
            if font.getlength(probe) <= width:
                line = probe
                continue
            if line:
                rows.append(line)
            line = ch
        rows.append(line)
    return rows or [""]


def render_card_png(title: str, body: str, *, width: int = CARD_WIDTH,
                    max_chars: int = MAX_CARD_CHARS) -> tuple[bytes, bool]:
    """把标题 + 正文画成一张 PNG（超长自动分页，最多 ``CARD_MAX_PAGES`` 页）。

    返回 ``(png 字节, 是否被截断)``。**纯函数**：不写盘。
    """
    from PIL import Image, ImageDraw

    text = str(body or "").strip()
    if not text:
        raise ArtifactError("empty_body")
    truncated = len(text) > max_chars
    if truncated:
        text = text[: max_chars - 1] + "…"

    body_font = _font(20)
    title_font = _font(26)
    inner = max(1, int(width) - 2 * CARD_PAD)
    rows = _wrap(text, body_font, inner)

    # 每页能放多少行：整张图高度按 3 页封顶。
    per_page = max(6, (CARD_LINE_H * 18))
    pages = [rows[i:i + per_page] for i in range(0, len(rows), per_page)] or [[""]]
    if len(pages) > CARD_MAX_PAGES:
        pages = pages[:CARD_MAX_PAGES]
        truncated = True

    height = CARD_PAD * 2 + 44 + CARD_LINE_H * per_page + 8
    canvas = Image.new("RGB", (int(width), height), (10, 14, 22))
    draw = ImageDraw.Draw(canvas)
    y = CARD_PAD
    draw.text((CARD_PAD, y), accompany_text(title), font=title_font,
              fill=(168, 226, 255))
    y += 40
    for idx, page in enumerate(pages, 1):
        for row in page:
            draw.text((CARD_PAD, y), row, font=body_font, fill=(232, 238, 246))
            y += CARD_LINE_H
        if len(pages) > 1:
            draw.text((int(width) - CARD_PAD - 20, height - CARD_PAD - 14),
                      f"{idx}/{len(pages)}", font=_font(14), fill=(120, 132, 150))

    from io import BytesIO

    out = BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue(), truncated


def _target_path(out_dir: str, name: str, suffix: str, *, turn_id: str = "") -> Path:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{slugify(name)}"
    if turn_id:
        tail = hashlib.sha256(f"{turn_id}:{name}:{stamp}".encode("utf-8")).hexdigest()[:6]
        base = f"{base}-{tail}"
    candidate = root / f"{base}{suffix}"
    seq = 2
    while candidate.exists():
        candidate = root / f"{base}-{seq}{suffix}"
        seq += 1
    return candidate


def write_card(title: str, body: str, *, out_dir: str = ARTIFACT_DIR,
               turn_id: str = "") -> str:
    """画卡片并落盘，返回**绝对路径**。"""
    png, _truncated = render_card_png(title, body)
    path = _target_path(out_dir, title, ".png", turn_id=turn_id)
    path.write_bytes(png)
    return str(path.resolve())


def write_page(title: str, body: str, *, out_dir: str = ARTIFACT_DIR,
               turn_id: str = "") -> str:
    """写一页 markdown（带标题与时间戳），返回**绝对路径**。"""
    text = str(body or "").strip()
    if not text:
        raise ArtifactError("empty_body")
    truncated = len(text) > MAX_PAGE_CHARS
    if truncated:
        text = text[: MAX_PAGE_CHARS - 1] + "…"
    head = str(title or "").strip() or "笔记"
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    content = f"# {head}\n\n> 鲸鲸生成 · {stamp}\n\n{text}\n"
    path = _target_path(out_dir, head, ".md", turn_id=turn_id)
    path.write_text(content, encoding="utf-8")
    return str(path.resolve())


def to_deliver(kind: str, *, title: str = "", body: str = "", url: str = "",
               path: str = "", target: str = "", basis: Sequence[str] = (),
               out_dir: str = ARTIFACT_DIR, turn_id: str = "") -> DeliverItem:
    """把「要交付什么」翻译成出口认识的那一条（``DeliverItem``）。

    - ``text``：正文就是消息本身，没有文件
    - ``card``：画一张图，消息里只带 ≤40 字伴随语
    - ``page``：写一页 md，消息里只带 ≤40 字伴随语
    - ``link``：消息就是一个链接（不做短链、不加引流语）
    - ``file``：只认**已存在**的本地路径；不存在直接抛（不许伪造一个路径出去）
    """
    shape = check_kind(kind)
    if shape == "text":
        if not str(body or "").strip():
            raise ArtifactError("empty_body")
        return DeliverItem(kind="say", target=target, text=str(body).strip(),
                           basis=tuple(basis))
    if shape == "link":
        link = str(url or "").strip()
        if not link.startswith(("http://", "https://")):
            raise ArtifactError("bad_url")
        return DeliverItem(kind="say", target=target, text=link, basis=tuple(basis))
    if shape == "file":
        real = Path(str(path or "")).expanduser()
        if not real.exists():
            raise ArtifactError("file_missing")
        return DeliverItem(kind="file", target=target, text=accompany_text(title),
                           file=str(real.resolve()), basis=tuple(basis))
    if shape == "card":
        return DeliverItem(kind="file", target=target, text=accompany_text(title),
                           file=write_card(title, body, out_dir=out_dir, turn_id=turn_id),
                           basis=tuple(basis))
    return DeliverItem(kind="file", target=target, text=accompany_text(title),
                       file=write_page(title, body, out_dir=out_dir, turn_id=turn_id),
                       basis=tuple(basis))


def artifact_of(kind: str, *, renderer: str = "", ttl_s: int | None = None):
    """造一个 ``contracts.plan.Artifact``（那一槽的形状）。"""
    from core.contracts.plan import Artifact

    return Artifact(kind=check_kind(kind), renderer=renderer, ttl_s=ttl_s)


def describe(item: DeliverItem) -> str:
    """回执/日志用的一行话：交付了什么，多大。"""
    if item.file:
        try:
            size = Path(item.file).stat().st_size
        except OSError:
            warn("core/artifacts/render.py:260 OSError", err=repr(__import__("sys").exc_info()[1]))
            size = 0
        return f"{item.kind}: {Path(item.file).name} ({size} B) 伴随语={len(item.text)} 字"
    return f"{item.kind}: {len(item.text)} 字"
