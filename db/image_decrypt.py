from __future__ import annotations

import structlog
from safelog import warn

log = structlog.get_logger()

_IMG_HEADS = [
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF8", "gif"),
    (b"RIFF", "webp"),
    (b"BM", "bmp"),
]


from core.infra import egress


def decrypt_dat(data: bytes) -> tuple[bytes | None, str]:
    if len(data) >= 6 and data[2:4] == b"V2" or data[2:4] == b"V1":
        return _decrypt_dat_v2(data)

    for head, ext in _IMG_HEADS:
        key = data[0] ^ head[0]
        match = True
        for i in range(1, len(head)):
            if (data[i] ^ key) != head[i]:
                match = False
                break
        if match:
            decrypted = bytes(b ^ key for b in data)
            return decrypted, ext

    return None, ""


def _decrypt_dat_v2(data: bytes) -> tuple[bytes | None, str]:
    import struct

    if len(data) < 15:
        return None, ""

    aes_size, xor_size = struct.unpack_from("<II", data, 6)
    offset = 15

    aligned_aes_size = aes_size
    if aligned_aes_size % 16 != 0:
        aligned_aes_size += 16 - (aligned_aes_size % 16)

    if offset + aligned_aes_size + xor_size > len(data):
        return None, ""

    aes_data = data[offset:offset + aligned_aes_size]
    xor_data = data[offset + aligned_aes_size:offset + aligned_aes_size + xor_size]

    from db.image_key import get_aes_key, get_xor_key
    aes_key = get_aes_key()
    xor_key = get_xor_key()

    if not aes_key:
        return None, ""

    try:
        from Cryptodome.Cipher import AES
        cipher = AES.new(aes_key[:16], AES.MODE_ECB)
        dec_aes = cipher.decrypt(aes_data)
        pad_len = dec_aes[-1]
        if 1 <= pad_len <= 16 and all(b == pad_len for b in dec_aes[-pad_len:]):
            dec_aes = dec_aes[:-pad_len]
    except Exception as _:
        warn("db/image_decrypt.py:67 Exception", err=repr(_))
        return None, ""

    dec_xor = bytes(b ^ xor_key for b in xor_data)
    # 2026-10-02 修复（live 缺陷"她看不见图"，24/24 实测 + 2 位独立子代理互证）：
    # 头部声明的 `aes_size + xor_size` **不是文件全部**——微信的 V2 `.dat` 在声明区之后
    # 还有一段（实测一律 **16 字节**）同样是 XOR 加密的尾巴，它解出来正好以 JPEG 的
    # 结束标记 `ff d9` 收尾。旧实现只取声明区、**把这 16 字节直接丢掉**，于是每张图都
    # "truncated (12–33 bytes not processed)"：Pillow 解不开、DeepSeek 视觉判
    # "unsupported image" → 她只能说"图片没读出来"。
    # 证据：`b3186e…_b.dat` 127715 B，声明区 127699 B，源尾 16 B 经 xor_key(0x4b) 解出
    # `…9d7fffd9`；补回后 Pillow 完整解出 500×333（抽查的 24 张全部同样成立）。
    # 反证"拷贝/缓存有损"：本函数输出与缓存件 md5 相同 —— 丢尾就发生在这里。
    tail_data = data[offset + aligned_aes_size + xor_size:]
    if tail_data:
        dec_xor += bytes(b ^ xor_key for b in tail_data)
    full_img = dec_aes + dec_xor

    for head, ext in _IMG_HEADS:
        if full_img[:len(head)] == head:
            return full_img, ext

    if full_img[:4] == b"wxgf":
        return full_img, "wxgf"

    return None, ""


def decode_wxgf(wxgf_data: bytes) -> bytes | None:
    for i in range(min(len(wxgf_data), 256)):
        if wxgf_data[i:i+4] == b'\x00\x00\x00\x01' or wxgf_data[i:i+3] == b'\x00\x00\x01':
            if wxgf_data[i:i+4] == b'\x00\x00\x00\x01':
                hevc_data = wxgf_data[i:]
            else:
                hevc_data = wxgf_data[i:]
            break
    else:
        return None

    import subprocess
    import tempfile

    try:
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        warn("db/image_decrypt.py:112 ImportError", err=repr(__import__("sys").exc_info()[1]))
        return None

    with tempfile.NamedTemporaryFile(suffix=".hevc", delete=False) as f:
        f.write(hevc_data)
        hevc_path = f.name

    out_path = hevc_path.replace(".hevc", ".png")

    try:
        result = subprocess.run(
            [ffmpeg_exe, "-y", "-f", "hevc", "-i", hevc_path,
             "-frames:v", "1", out_path],
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0:
            from pathlib import Path as _Path
            out_data = _Path(out_path).read_bytes()
            if out_data[:4] == b"\x89PNG":
                return out_data
    except Exception as _:
        warn("db/image_decrypt.py:133 Exception", err=repr(_))
    finally:
        from pathlib import Path as _Path
        _Path(hevc_path).unlink(missing_ok=True)
        _Path(out_path).unlink(missing_ok=True)

    return None


def decrypt_emoji_file(data: bytes, md5: str) -> tuple[bytes, str] | None:
    if data[:3] == b"\xff\xd8\xff":
        return data, "jpg"
    if data[:4] == b"\x89PNG":
        return data, "png"
    if data[:4] == b"GIF8":
        return data, "gif"
    if data[:4] == b"RIFF":
        return data, "webp"

    aes_key = _get_emoji_aes_key(md5)
    if aes_key:
        try:
            from Cryptodome.Cipher import AES
            pad_len = 16 - (len(data) % 16) if len(data) % 16 else 0
            padded = data + b"\x00" * pad_len
            cipher = AES.new(aes_key, AES.MODE_ECB)
            dec = cipher.decrypt(padded)[: len(data)]
            if dec[:3] == b"\xff\xd8\xff":
                return dec, "jpg"
            if dec[:4] == b"\x89PNG":
                return dec, "png"
            if dec[:4] == b"GIF8":
                return dec, "gif"
            if dec[:4] == b"RIFF":
                return dec, "webp"
        except Exception as _:
            warn("db/image_decrypt.py:169 Exception", err=repr(_))

    cdn_data = _download_emoji_cdn(md5)
    if cdn_data:
        return cdn_data[0], cdn_data[1]

    return None


def _emoji_db_candidates():
    """返回可能包含表情包 AES key / CDN URL 的解密 DB 路径。"""
    from pathlib import Path as _Path

    candidates = []
    try:
        from core.config.settings import config

        root = _Path(config.paths.decrypted_dir)
        candidates.extend(sorted(root.glob("wechat/*/emoticon_emoticon.db")))
        candidates.append(root / "emoticon_emoticon.db")
    except Exception:
        warn("db/image_decrypt.py:190 Exception", err=repr(__import__("sys").exc_info()[1]))

    candidates.extend(sorted(_Path("data/decrypted/wechat").glob("*/emoticon_emoticon.db")))
    candidates.append(_Path("data/_debug_dec/emoticon_emoticon.db"))

    seen = set()
    result = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.exists():
            result.append(path)
    return result


def _query_emoji_db(md5: str, column: str) -> str | None:
    import sqlite3

    for emoji_db in _emoji_db_candidates():
        try:
            conn = sqlite3.connect(f"file:{emoji_db}?mode=ro", uri=True)
            c = conn.cursor()
            row = c.execute(
                f"SELECT {column} FROM kNonStoreEmoticonTable WHERE md5 = ?",
                (md5,),
            ).fetchone()
            conn.close()
            if row and row[0]:
                return row[0]
        except Exception:
            warn("db/image_decrypt.py:222 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
    return None


def _get_emoji_aes_key(md5: str) -> bytes | None:
    key_str = _query_emoji_db(md5, "aes_key")
    if not key_str:
        return None
    if len(key_str) == 32:
        return bytes.fromhex(key_str)
    if len(key_str) >= 16:
        return key_str[:16].encode("ascii")
    return None


def _download_emoji_cdn(md5: str) -> tuple[bytes, str] | None:
    url = _query_emoji_db(md5, "cdn_url") or _query_emoji_db(md5, "thumb_url")
    if not url:
        return None

    try:
        with egress.client(timeout_ms=15_000) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.content
        if data[:3] == b"\xff\xd8\xff":
            return data, "jpg"
        if data[:4] == b"\x89PNG":
            return data, "png"
        if data[:4] == b"GIF8":
            return data, "gif"
        if data[:4] == b"RIFF":
            return data, "webp"
        return data, "jpg"
    except Exception:
        warn("db/image_decrypt.py:258 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None
