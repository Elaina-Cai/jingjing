"""把图片/文件放进剪贴板，让微信用 Ctrl+V 直接发出去。

微信 PC 版：剪贴板里是「位图」→ 当图片发（只取第一帧）；剪贴板里是「文件」
→ 当文件发。键盘打字只能发文字，所以发图必须走这两条剪贴板通道。

2026-10-27 立：原先 wx_sender.send_file() 是把文件路径当文字打进去再回车，
发出去的是路径字符串而不是文件，这条路是错的，这里补上正确做法。
"""
from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path
from struct import pack

_IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}


def put_image_on_clipboard(filepath: str) -> bool:
    """png/jpg/gif 的第一帧 → CF_DIB。返回是否成功。"""
    p = Path(str(filepath or ""))
    if not p.is_file() or p.suffix.lower() not in _IMG_EXT:
        return False
    try:
        from PIL import Image
        import win32clipboard
    except Exception:
        return False
    with Image.open(p) as im:
        frame = im.convert("RGB")
        buf = BytesIO()
        frame.save(buf, "BMP")
    dib = buf.getvalue()[14:]  # 去掉 14 字节 BITMAPFILEHEADER，CF_DIB 只要 DIB
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return False
    return True


def put_file_on_clipboard(filepath: str) -> bool:
    """一个文件 → CF_HDROP（等同于在资源管理器里 Ctrl+C 这个文件）。"""
    p = os.path.abspath(str(filepath or ""))
    if not os.path.isfile(p):
        return False
    try:
        import win32clipboard
    except Exception:
        return False
    # DROPFILES：pFiles=20, pt=(0,0), fNC=0, fWide=1，随后是宽字符路径表
    header = pack("<IiiII", 20, 0, 0, 0, 1)
    payload = header + (p + "\0\0").encode("utf-16-le")
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, payload)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return False
    return True


__all__ = ["put_image_on_clipboard", "put_file_on_clipboard"]
