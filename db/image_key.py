import hashlib
import re
from pathlib import Path
from safelog import warn

_cached_keys: dict | None = None


def get_image_keys() -> dict:
    global _cached_keys
    if _cached_keys is not None:
        return _cached_keys

    keys = _compute_image_keys()
    _cached_keys = keys
    return keys


def invalidate_cache():
    global _cached_keys
    _cached_keys = None


def _compute_image_keys() -> dict:
    result: dict = {}

    try:
        from core.config.settings import config
        manual_key = config.wechat.image_aes_key
        if manual_key and len(manual_key) >= 16:
            result["aes_key"] = manual_key[:16].encode("ascii")
            if len(manual_key) > 16:
                try:
                    result["xor_key"] = int(manual_key[16:]) & 0xFF
                except ValueError:
                    warn("db/image_key.py:34 ValueError", err=repr(__import__("sys").exc_info()[1]))
            return result
    except Exception as _:
        warn("db/image_key.py:37 Exception", err=repr(_))

    wxid = _get_wxid()
    code = _get_unique_code()

    if code is not None and wxid:
        xor_key = code & 0xFF
        data_to_hash = str(code) + wxid
        md5_hex = hashlib.md5(data_to_hash.encode()).hexdigest()
        aes_key = md5_hex[:16].encode("ascii")

        result["aes_key"] = aes_key
        result["xor_key"] = xor_key
        result["code"] = code
        result["wxid"] = wxid

    return result


def _get_wxid(wx_db_path: str = "") -> str:
    try:
        if not wx_db_path:
            from core.config.settings import config
            wx_db_path = config.wechat.db_path or ""
        if wx_db_path:
            m = re.search(r"(wxid_[a-zA-Z0-9]+)", wx_db_path)
            if m:
                raw = m.group(1)
                first_underscore = raw.find("_")
                if first_underscore != -1:
                    second_underscore = raw.find("_", first_underscore + 1)
                    if second_underscore != -1:
                        return raw[:second_underscore]
                return raw
    except Exception as _:
        warn("db/image_key.py:72 Exception", err=repr(_))
    return ""


def _get_unique_code() -> int | None:
    home = Path.home()
    cache_dir = home / "AppData" / "Roaming" / "Tencent" / "xwechat" / "net" / "kvcomm"

    if not cache_dir.exists():
        return None

    pattern = re.compile(r"key_(\d+)_.*\.statistic")
    codes: list[int] = []

    try:
        for f in cache_dir.iterdir():
            m = pattern.match(f.name)
            if m:
                code = int(m.group(1))
                if 0 < code <= 4294967295:
                    codes.append(code)
    except Exception as _:
        warn("db/image_key.py:94 Exception", err=repr(_))

    if not codes:
        return None

    return max(codes)


def get_aes_key() -> bytes | None:
    keys = get_image_keys()
    return keys.get("aes_key")


def get_xor_key() -> int:
    keys = get_image_keys()
    return keys.get("xor_key", 0)
