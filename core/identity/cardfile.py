"""角色卡正文的写回契约（CB-069）。

卡是「一整块自由文本」，程序只认头部三行、正文一个字不改地整块注入。所以「写回」
这件事必须有**一处**定义：换行怎么统一、尾部空行怎么处理、上限按什么算、
落盘前要不要看素材。散在 `console/executor.py` 里就必然各写一遍（面板改卡、
复制卡、将来导入，各一份）。

三条纪律（都是踩过的）：
1. **不许 `strip()` 整块正文**。`:2416` 的老写法 `str(patch["text"]).strip()` 顺手吃掉了
   最后一行行尾的两个空格 —— markdown 里那是硬换行，属于内容不是排版。
2. **只有空格/Tab 的行原样保留**。只去掉「尾部连续的空白行」，别动行内。
3. **上限只在这里定**：字节（UTF-8，128KB）是拦的，字数（20000）只是提示线。
   两个数各自的口径写在这里，别让上游各理解一次。
"""

from __future__ import annotations

import os
from typing import Any

MAX_BYTES = 131072   # 128KB，UTF-8 字节：硬上限（拦）
MAX_CHARS = 20000    # 20000 字：提示线（不拦，只提醒前缀越来越贵）
HEADER = ("name", "voice", "looks")   # 头部三行，其余键原样保留


def normalise(text: Any) -> str:
    """统一换行（CRLF/CR → LF），去掉尾部连续空白行；行内空格与行尾两空格保留。"""
    t = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = t.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def check(body: str) -> dict:
    """量一次正文：字节超了是错，字数超了只是提醒。"""
    t = normalise(body)
    b = len(t.encode("utf-8"))
    c = len(t)
    errors, warnings = [], []
    if b > MAX_BYTES:
        errors.append("正文 %d 字节，超过上限 %d 字节（UTF-8）" % (b, MAX_BYTES))
    if c > MAX_CHARS:
        warnings.append("正文 %d 字，超过提示线 %d 字（不拦，但前缀越长，缓存越贵）"
                        % (c, MAX_CHARS))
    return {"ok": not errors, "bytes": b, "chars": c,
            "errors": errors, "warnings": warnings}


def assets(slug: str = "", *, kinds=("faces", "voice")) -> dict:
    """卡头 `looks`/`voice` 指向的素材：缺目录、许可不让分发，都只是提醒（存卡不拦，出包才拦）。

    注意 `core.assets.paths.export_blockers()` **抓不到「素材不存在」**：它在
    `kind_dir(...) is None` 时直接 `continue`（没有素材就没有许可问题）。所以在
    这里把两件事分开问：缺什么 → `absent`/`warnings`；许可 → `blockers`。
    """
    slug = str(slug or "").strip()
    if not slug:
        return {"slug": "", "absent": [], "warnings": [], "blockers": []}
    try:
        from core.assets import paths as _ap
    except Exception as exc:  # noqa: BLE001
        return {"slug": slug, "absent": [], "warnings": [
            "素材层不可用（%s: %s），这次没体检" % (type(exc).__name__, exc)], "blockers": []}
    absent, warnings = [], []
    for kind in tuple(kinds or ()):
        if _ap.kind_dir(slug, kind) is None:
            absent.append(str(kind))
            warnings.append("素材里还没有 %s/%s：卡头写了 %s=%s，但目录不存在（可以先写卡、后补素材）"
                            % (slug, kind, kind, slug))
    blockers = []
    try:
        for b in _ap.export_blockers(slug, kinds=tuple(kinds or ())) or []:
            blockers.append({"kind": b.get("kind"), "reason": b.get("reason"),
                             "source": b.get("source"), "note": b.get("note")})
    except Exception as exc:  # noqa: BLE001
        warnings.append("许可检查失败（%s: %s），这次没体检" % (type(exc).__name__, exc))
    return {"slug": slug, "absent": absent, "warnings": warnings, "blockers": blockers}


def join(meta: dict, body: str) -> str:
    """头部三行 + `---` + 正文 + 单个结尾换行（与磁盘上的卡同形）。"""
    head = ["%s: %s" % (k, str((meta or {}).get(k) or "").strip()) for k in HEADER]
    return "\n".join(head + ["---"]) + "\n" + normalise(body) + "\n"


def write(path: str, meta: dict, body: str, *, dry_run: bool = False) -> dict:
    """走完契约再落盘：规范化 → 量大小 → 素材体检 → 原子写。"""
    body = normalise(body)
    if not body:
        return {"ok": False, "error": "卡不能是空的（整张卡就是一块提示词）。"}
    rep = check(body)
    if rep["errors"]:
        return {"ok": False, "error": "；".join(rep["errors"]), "check": rep}
    found = assets((meta or {}).get("looks"))
    # 素材许可不许出包 → 只提醒，不拦存卡。卡里只是一句 slug，许可是出包时的事；
    # 真拦会把鲸鲸那张卡直接锁死（faces/voice 都是 license_forbids），等于改不了她的卡。
    for b in found["blockers"]:
        found["warnings"].append("素材许可不允许出包：%s(%s)，出处 %s —— 卡能存，打包时会被跳过"
                                 % (b.get("kind"), b.get("reason"), b.get("source") or "未写"))
    text = join(meta, body)
    result = {"ok": True, "bytes": rep["bytes"], "chars": rep["chars"],
              "warnings": rep["warnings"] + found["warnings"], "text": text}
    if dry_run:
        result["dry_run"] = True
        return result
    from core.infra.jsonio import atomic_write_text
    if not atomic_write_text(path, text):
        return {"ok": False, "error": "写盘失败：%s" % os.path.relpath(str(path))}
    result["dry_run"] = False
    return result
