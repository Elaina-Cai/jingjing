from __future__ import annotations

import re

import structlog
from safelog import warn

log = structlog.get_logger()


def _read_varint(data: bytes, pos: int) -> tuple[int | None, int]:
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return result, pos
        shift += 7
        if shift >= 64:
            return None, pos
    return None, pos


def looks_like_wxid(s: str) -> bool:
    if not s or len(s) < 4:
        return False
    if s.startswith("wxid_"):
        return True
    if s.endswith("@chatroom"):
        return True
    if s.startswith("gh_"):
        return True
    if s.startswith("v1_"):
        return True
    return False


def extract_link_url(wx_msg) -> str | None:
    try:
        import xml.etree.ElementTree as ET
        raw = wx_msg.str_content or wx_msg.compress_content or b""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not raw.strip().startswith("<"):
            return None
        root = ET.fromstring(raw)
        url = root.get("url") or ""
        if not url:
            for tag in root.iter():
                url = tag.get("url", "")
                if url:
                    break
        return url or None
    except Exception as _:
        warn("db/parser.py:55 Exception", err=repr(_))
        return None


def extract_link_title(wx_msg) -> str | None:
    try:
        import xml.etree.ElementTree as ET
        raw = wx_msg.str_content or wx_msg.compress_content or b""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not raw.strip().startswith("<"):
            return None
        root = ET.fromstring(raw)
        title = root.get("title") or ""
        if not title:
            for tag in root.iter():
                title = tag.get("title", "")
                if title:
                    break
        return title or None
    except Exception as _:
        warn("db/parser.py:75 Exception", err=repr(_))
        return None


def parse_packed_info(data: bytes) -> list[str]:
    wxids: list[str] = []
    if not data:
        return wxids
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        if tag is None:
            break
        wire_type = tag & 0x7
        if wire_type == 0:
            _, pos = _read_varint(data, pos)
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            if length is None or pos + length > len(data):
                break
            val = data[pos:pos + length]
            pos += length
            try:
                s = val.decode("utf-8")
                if looks_like_wxid(s):
                    wxids.append(s)
            except Exception as _:
                warn("db/parser.py:101 Exception", err=repr(_))
        elif wire_type == 5:
            pos += 4
        elif wire_type == 1:
            pos += 8
        else:
            break
    return wxids


def extract_group_sender(content: str) -> tuple[str, str]:
    if not content:
        return "", content
    if ":\n" in content:
        parts = content.split(":\n", 1)
        sender = parts[0].strip()
        text = parts[1] if len(parts) > 1 else ""
        if looks_like_wxid(sender):
            return sender, text
    return "", content


# A pat's wire content is a rendered title, not a message body:
#   "\"P5\" 拍了拍 \"鲸鲸\""                      group, plain
#   "\"P5\" 拍了拍 \"鲸鲸\" 你给我来点钱"          group, with a suffix
#   "\"Elaina\" 拍了拍我"                          private (local account is 我)
#   "我拍了拍 \"P5\""                             self-sent
# There is no `wxid:\n` prefix and no wxid anywhere in it, so
# ``extract_group_sender`` returns nothing and every pat collapsed onto one
# "unknown" identity.  That made per-speaker pat counting impossible -- the
# names were only ever in this title.  The speaker is the quoted field BEFORE
# 拍了拍; the field after it is the pat's target, not its author.
_PAT_SENDER_RE = re.compile(r'^\s*(?P<sender>"[^"]*"|[^\s"]+)\s*拍了拍\s*')


def _pat_title_text(content) -> str:
    """The raw title text of a pat message, with any ``<title>`` unwrapped."""
    if not content:
        return ""
    text = str(content)
    if "<title>" in text:
        try:
            title_match = re.search(r"<title>(.*?)</title>", text, re.DOTALL)
            if title_match:
                text = title_match.group(1)
        except Exception:
            warn("db/parser.py:147 Exception", err=repr(__import__("sys").exc_info()[1]))
    return text.strip()


def is_pat_title(content) -> bool:
    """Whether this content is a pat title, whoever sent it.

    Distinct from :func:`extract_pat_sender` on purpose: a pat we sent
    ourselves (``我拍了拍 "P6"``) has *no* attributable author and still
    returns "" from the parser, yet it is unmistakably a pat.  Callers that
    only need to know "is this a pat, and therefore not somebody's own words"
    want this predicate instead.
    """
    return bool(_PAT_SENDER_RE.match(_pat_title_text(content)))


def extract_pat_sender(content: str) -> str:
    """The display name of whoever sent a pat, or "" when it is not a pat.

    Returns a *name*, never a wxid: the wire format carries no id, so the
    caller has to resolve the name to a person through the identity layer.
    Never guesses -- an unparseable title yields "" rather than a wrong name.
    """
    match = _PAT_SENDER_RE.match(_pat_title_text(content))
    if not match:
        return ""
    sender = match.group("sender").strip().strip('"').strip()
    # "我拍了拍…" is the local account talking, not somebody else.
    if sender in ("我", "自己"):
        return ""
    return sender


_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def ensure_str(val) -> str:
    if val is None:
        return ""
    if isinstance(val, bytes):
        if val[:4] == _ZSTD_MAGIC:
            try:
                import zstandard
                dctx = zstandard.ZstdDecompressor()
                val = dctx.decompress(val, max_output_size=1024 * 1024)
            except Exception as _:
                warn("db/parser.py:193 Exception", err=repr(_))
                return ""
        try:
            return val.decode("utf-8", errors="replace")
        except Exception as _:
            warn("db/parser.py:197 Exception", err=repr(_))
            return ""
    return str(val)


def resolve_msg_type_name(wx_msg) -> str:
    from core.execution.provider import PlatformTypeMapper
    return PlatformTypeMapper.from_wechat(wx_msg.type, wx_msg.sub_type).value
