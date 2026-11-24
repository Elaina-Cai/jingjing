import re

import lz4.block
import xml.etree.ElementTree as ET
import structlog
from db.wx_schema import WxMessage, WxMsgType, WxSubType
from safelog import warn
from core.infra.warn_guard import once

log = structlog.get_logger()

# A 32-character hex run that is not part of a longer hex run.
_HEX32_RE = re.compile(rb"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")


def extract_text(msg: WxMessage) -> str:
    if msg.type == WxMsgType.TEXT:
        content = msg.str_content or ""
        if isinstance(content, bytes):
            try:
                content = content.decode("utf-8", errors="replace")
            except Exception as _:
                warn("db/content_extract.py:20 Exception", err=repr(_))
                content = ""
        return content

    if msg.type == WxMsgType.IMAGE:
        return "[图片]"

    if msg.type == WxMsgType.EMOJI:
        return _extract_emoji(msg)

    if msg.type == WxMsgType.SYSTEM:
        content = msg.display_content or msg.str_content or ""
        if isinstance(content, bytes):
            try:
                content = content.decode("utf-8", errors="replace")
            except Exception as _:
                warn("db/content_extract.py:35 Exception", err=repr(_))
                content = ""
        return content

    if msg.type == WxMsgType.PAT:
        return _extract_pat(msg)

    if msg.type == WxMsgType.SHARE_LINK:
        return _extract_type49(msg)

    return ""


def extract_media_path(msg: WxMessage) -> str:
    """从 bytes_extra 提取图片/表情包的文件路径。

    对于表情包(msg_type=47)：从XML元数据提取MD5，搜索本地缓存文件。
    对于图片(msg_type=3)：微信4.x中bytes_extra存的是MD5哈希而非路径，
    优先搜索cache目录下的缩略图(jpg/png)，其次搜索msg/attach下的.dat文件。
    """
    if msg.type not in (WxMsgType.IMAGE, WxMsgType.EMOJI):
        return ""

    if msg.type == WxMsgType.EMOJI:
        meta = extract_emoji_meta(msg)
        md5_val = meta.get("md5", "")
        if md5_val:
            local = _resolve_emoji_local(md5_val)
            if local:
                return local
        return ""

    raw = msg.bytes_extra or b""
    if not raw:
        return ""

    all_strings = _extract_all_strings(raw)

    for s in all_strings:
        if not s:
            continue
        if "\\" in s or "/" in s:
            if any(s.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")):
                return s
            if "MsgFile" in s or "Image" in s or "Emoji" in s or "FileStorage" in s:
                return s

    md5_val = _extract_image_md5(all_strings)
    if not md5_val:
        # Some payload variants carry the id where the field walker cannot see
        # it; the raw scan is what makes resolution work for those messages.
        md5_val = _md5_from_raw_bytes(raw)
    if md5_val:
        thumb = _resolve_image_thumbnail(md5_val)
        if thumb:
            return thumb
        dat = _resolve_image_dat(md5_val)
        if dat:
            return dat

    return ""


def _md5_from_raw_bytes(raw: bytes) -> str:
    """Find an image id by scanning the protobuf instead of walking its fields.

    ``_extract_all_strings`` returns nothing at all for a payload variant that
    real messages do use -- measured on 8 image messages whose packed_info_data
    is 45-46 bytes and carries the md5 as an ASCII field behind a
    length-delimited wrapper the walker never enters (it parses the 42-byte
    variant of the same field fine).  Their image files were all on disk; only
    the clue was unreachable, so those pictures were invisible forever.

    Both known variants carry the id as 32 hex characters, which is unambiguous
    in this payload, so scan for that rather than trusting the walk.
    """
    for match in _HEX32_RE.finditer(raw or b""):
        value = match.group().decode("ascii", "ignore").lower()
        # A real image id is a digest; a single repeated character is not.
        if len(set(value)) > 1:
            return value
    return ""


def md5_from_packed_info(raw) -> str:
    """Best-effort image id from a raw ``packed_info_data`` payload."""
    if not raw:
        return ""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    found = ""
    try:
        found = _extract_image_md5(_extract_all_strings(raw))
    except Exception:
        warn("db/content_extract.py:128 Exception", err=repr(__import__("sys").exc_info()[1]))
        found = ""
    return found or _md5_from_raw_bytes(raw)


def extract_image_md5(msg: WxMessage) -> str:
    """The md5 WeChat records for an image or emoji message, if it records one.

    Kept separate from ``extract_media_path`` on purpose.  That function answers
    "can I give you a file right now", and it legitimately fails when WeChat has
    not written the file yet -- measured at 1.1s for one image and 27s for
    another.  This one answers "what would I look for later", and must succeed
    even then, because the md5 is the only thing that makes a retry possible.
    Throwing it away is what made a single unlucky moment permanent.
    """
    if msg.type == WxMsgType.EMOJI:
        try:
            return str(extract_emoji_meta(msg).get("md5") or "")
        except Exception:
            warn("db/content_extract.py:146 Exception", err=repr(__import__("sys").exc_info()[1]))
            return ""
    if msg.type != WxMsgType.IMAGE:
        return ""
    return md5_from_packed_info(msg.bytes_extra or b"")


def _extract_image_md5(strings: list[str]) -> str:
    """从protobuf提取的字符串列表中识别图片MD5。

    微信4.x图片消息的packed_info_data中，MD5格式如 '" 0351eb5be5b0ba077415f48d432a00a1'。
    """
    import re
    md5_pattern = re.compile(r"[0-9a-f]{32}")
    for s in strings:
        s = s.strip().strip('"').strip()
        if md5_pattern.fullmatch(s):
            return s
    return ""


def _resolve_image_thumbnail(md5: str, wx_db_path: str = "") -> str:
    """在微信cache目录下搜索已解密的图片资源。

    搜索优先级：
    1. HttpResource/{md5} - 已解密的HTTP图片资源(PNG/JPG)
    2. Message/{chatroom}/Bubble/{md5}_b.dat - V2加密的图片气泡(需后续解密)
    """
    from pathlib import Path
    try:
        from core.config.settings import config
        db_path = wx_db_path or config.wechat.db_path or ""
    except Exception as _:
        warn("db/content_extract.py:178 Exception", err=repr(_))
        db_path = wx_db_path
    if not db_path:
        return ""

    base_dir = Path(db_path).parent
    cache_dir = base_dir / "cache"
    if not cache_dir.exists():
        return ""

    for http_res in cache_dir.rglob(md5):
        if http_res.is_file() and http_res.stat().st_size > 0:
            header = http_res.read_bytes()[:8]
            if header[:3] == b"\xff\xd8\xff" or header[:4] == b"\x89PNG" or header[:4] == b"GIF8":
                return str(http_res)

    for bubble in cache_dir.rglob(f"{md5}_b.dat"):
        if bubble.is_file() and bubble.stat().st_size > 0:
            return str(bubble)

    return ""


def _resolve_image_dat(md5: str, wx_db_path: str = "") -> str:
    """根据MD5在微信数据目录的msg/attach下搜索对应的.dat文件。"""
    from pathlib import Path
    try:
        from core.config.settings import config
        db_path = wx_db_path or config.wechat.db_path or ""
    except Exception as _:
        warn("db/content_extract.py:207 Exception", err=repr(_))
        db_path = wx_db_path
    if not db_path:
        return ""

    base_dir = Path(db_path).parent
    attach_dir = base_dir / "msg" / "attach"
    if not attach_dir.exists():
        return ""

    # 2026-10-27: 微信 4.x 经常只把缩略图落盘 —— msg/attach/<会话hash>/<YYYY-MM>/Img/
    # 下是 <md5>_t.dat（原图 <md5>.dat 可能压根没下载）。旧实现只找 <md5>.dat，
    # 于是图明明在硬盘上却被判成 "found no local file for md5"，她只能对一张
    # 一眼就懂的图说"什么都看不见"。原图优先，缩略图兜底（发信人看到的就是它）。
    for target_name in (f"{md5}.dat", f"{md5}_t.dat", f"{md5}_h.dat"):
        for dat_file in attach_dir.rglob(target_name):
            if dat_file.name == target_name and dat_file.stat().st_size > 0:
                return str(dat_file)

    return ""


def _resolve_emoji_local(md5: str, wx_db_path: str = "") -> str:
    """根据表情包MD5搜索本地缓存文件。

    搜索位置：
    1. data/image_cache/下已缓存的文件
    2. 微信cache/Emoticon/{md5[:2]}/{md5}
    3. 微信business/emoticon/Persist/{md5[:2]}/{md5}
    """
    from pathlib import Path
    try:
        from core.config.settings import config
        db_path = wx_db_path or config.wechat.db_path or ""
    except Exception as _:
        warn("db/content_extract.py:237 Exception", err=repr(_))
        db_path = wx_db_path
    if not db_path:
        return ""

    cache_root = Path("data/image_cache")
    if cache_root.exists():
        for f in cache_root.iterdir():
            if f.stem == md5 and f.stat().st_size > 0:
                return str(f)

    base_dir = Path(db_path).parent
    prefix = md5[:2]

    # 2026-10-27: 原实现把月份写死成 cache/2026-09 —— 9 月一过，所有表情包都被判
    # "找不到本地文件"，而且静默无日志。改成按实际目录 glob。
    for pattern in (f"cache/*/Emoticon/{prefix}/{md5}",
                    f"cache/Emoticon/{prefix}/{md5}",
                    f"business/emoticon/Persist/{prefix}/{md5}",
                    f"business/emoticon/Persist/{prefix}/*/{md5}"):
        for p in base_dir.glob(pattern):
            if p.is_file() and p.stat().st_size > 0:
                return str(p)

    return ""


def _extract_all_strings(data: bytes) -> list[str]:
    """解析 protobuf packed_info_data，提取所有字符串字段（包括路径和wxid）。"""
    from db.parser import _read_varint
    strings = []
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
                if len(s) > 2:
                    strings.append(s)
            except Exception as _:
                warn("db/content_extract.py:284 Exception", err=repr(_))
        elif wire_type == 5:
            pos += 4
        elif wire_type == 1:
            pos += 8
        else:
            break
    return strings


_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _decompress_bytes(data: bytes) -> str:
    """尝试用 Zstd 或 LZ4 解压字节，返回 UTF-8 字符串。"""
    if not data:
        return ""
    if len(data) >= 4 and data[:4] == _ZSTD_MAGIC:
        try:
            import zstandard
            dctx = zstandard.ZstdDecompressor()
            raw = dctx.decompress(data, max_output_size=1024 * 1024)
            return raw.decode("utf-8", errors="ignore")
        except Exception as _:
            warn("db/content_extract.py:308 Exception", err=repr(_))
    try:
        raw = lz4.block.decompress(data, uncompressed_size=1024 * 1024)
        return raw.decode("utf-8", errors="ignore")
    except Exception as _:
        warn("db/content_extract.py:313 Exception", err=repr(_))
        return ""


def _get_emoji_xml(msg: WxMessage) -> str:
    """获取表情包 XML 元数据字符串，兼容 Zstd/LZ4 压缩。"""
    xml_str = msg.str_content or ""
    if isinstance(xml_str, bytes):
        if len(xml_str) >= 4 and xml_str[:4] == _ZSTD_MAGIC:
            xml_str = _decompress_bytes(xml_str)
        else:
            try:
                xml_str = xml_str.decode("utf-8", errors="replace")
            except Exception as _:
                warn("db/content_extract.py:326 Exception", err=repr(_))
                xml_str = ""

    if not xml_str and msg.compress_content:
        xml_str = _decompress_bytes(msg.compress_content)

    return xml_str or ""


def extract_emoji_meta(msg: WxMessage) -> dict:
    """提取表情包元数据: md5、cdn_url、尺寸等，供后续下载与识别使用。"""
    xml_str = _get_emoji_xml(msg)
    if not xml_str:
        return {}
    # 2026-10-27: 不是 XML 就别当 XML 解析。以前每条这样的消息都写一行
    # `ET.ParseError` 到 silent_warnings.log，一小时能刷几十行，把真告警埋了。
    if not xml_str.lstrip().startswith("<"):
        return {}
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as _exc:
        if once("emoji_xml:" + str(getattr(msg, "local_id", "?"))[:0] + "parse", every_s=6 * 3600):
            warn("db/content_extract.py:342 ET.ParseError", err=repr(_exc))
        idx = xml_str.find("<msg>")
        if idx < 0:
            idx = xml_str.find("<emoji")
        if idx > 0:
            try:
                root = ET.fromstring(xml_str[idx:])
            except ET.ParseError:
                warn("db/content_extract.py:349 ET.ParseError", err=repr(__import__("sys").exc_info()[1]))
                return {}
        else:
            return {}
    emoji = root.find(".//emoji")
    if emoji is None:
        return {}
    cdn_url = (
        emoji.get("cdnurl", "")
        or emoji.get("externurl", "")
        or emoji.get("thumburl", "")
    )
    return {
        "md5": emoji.get("md5", ""),
        "cdn_url": cdn_url,
        "width": emoji.get("width", ""),
        "height": emoji.get("height", ""),
        "desc": emoji.get("desc", "") or emoji.get("attachedtext", ""),
    }


def _extract_emoji(msg: WxMessage) -> str:
    meta = extract_emoji_meta(msg)
    desc = meta.get("desc", "")
    if desc:
        return f"[表情包:{desc}]"
    md5 = meta.get("md5", "")
    if md5:
        return f"[表情包:{md5}]"
    return "[表情包]"


def _extract_type49(msg: WxMessage) -> str:
    xml_str = _decompress_xml(msg)
    if not xml_str:
        return ""

    # 2026-11-02：群里的 appmsg 有一部分把发送者前缀直接写进了正文：
    #   "wxid_xxx: <msg><appmsg>…"
    # ``extract_group_sender`` 只认 ``":\n"``（冒号+换行），而微信这里给的是
    # "冒号+空格"，于是前缀留在正文里，ET.fromstring 直接 ParseError，
    # 一路掉到 _fallback_extract 把 "<![CDATA[微信转账]]>" 当正文入库。
    # 实测 68 条转账里 54 条（79%）会变成这种垃圾。
    # 这里只做一件事：正文不是以 "<" 开头时，从第一个 "<" 处切开。
    # 纯文本消息没有 "<"，find 返回 -1，原样不动。
    stripped = xml_str.lstrip()
    if stripped and not stripped.startswith("<"):
        lt = xml_str.find("<")
        if lt > 0:
            xml_str = xml_str[lt:]

    xml_start = xml_str.find("<?xml")
    if xml_start > 0:
        xml_str = xml_str[xml_start:]

    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        warn("db/content_extract.py:392 ET.ParseError", err=repr(__import__("sys").exc_info()[1]))
        return _fallback_extract(xml_str)

    if msg.sub_type == WxSubType.MERGE_FORWARD:
        records = root.findall(".//record")
        if records:
            lines = []
            for r in records:
                title = r.findtext("title", "")
                if title:
                    lines.append(title)
            return "\n".join(lines)

    if msg.sub_type in (WxSubType.QUOTE_REPLY_33, WxSubType.QUOTE_REPLY_36, WxSubType.QUOTE_REPLY_57):
        title = root.findtext(".//title", "") or ""
        refer = root.find(".//refermsg")
        if refer is not None:
            ref_name = refer.findtext("displayname", "") or ""
            ref_content = refer.findtext("content", "") or ""
            from core.conversation.quote_text import clean_quote_text

            ref_content = clean_quote_text(ref_content)
            parts = []
            if ref_name or ref_content:
                parts.append(f"[引用 {ref_name}: {ref_content}]" if ref_name else f"[引用: {ref_content}]")
            if title.strip():
                parts.append(title.strip())
            return "\n".join(parts)
        return title

    if msg.sub_type in (WxSubType.OTHER_87,):
        title = root.findtext(".//title", "")
        return title

    if msg.sub_type == WxSubType.TRANSFER:
        # 微信给的 des 是「收到转账3.20元。如需收钱，请点此升级至最新版本」。
        # 后半句是客户端升级提示，不是对方说的话——留着会变成一句
        # "请点此升级" 的噪声混进上下文，所以只取第一句。
        title = root.findtext(".//title", "") or ""
        desc = (root.findtext(".//des", "") or "").split("如需")[0].strip().rstrip("。")
        return "\n".join(p for p in (title, desc) if p)

    title = root.findtext(".//title", "")
    desc = root.findtext(".//des", "")
    parts = [p for p in (title, desc) if p]
    return "\n".join(parts)


def is_pat_sent_by_self(content: str) -> bool:
    """拍一拍标题是否表示“本账号拍了拍对方”。

    - ``我拍了拍 "Elaina"``：本账号发出，不应触发回复；
    - ``"Elaina" 拍了拍我``：对方拍本账号，应触发回复。
    """
    return bool(content) and content.lstrip().startswith("我拍了拍")


def _extract_pat(msg: WxMessage) -> str:
    # 2026-11-02：以前直接读 msg.str_content，而微信 4.x 的 message_content 是 BLOB，
    # 拍一拍的正文常常是 bytes。拿字符串正则去 re.search(bytes) 会抛
    # TypeError: cannot use a string pattern on a bytes-like object。
    # 全库实测 735 条拍一拍会炸，而 sync_service 没有 try/except ——
    # 一条拍一拍就能带崩整轮同步（该会话的游标永远推不动）。
    # _decompress_xml 已经同时处理 bytes / zstd / 纯文本，直接复用。
    xml_str = _decompress_xml(msg)
    if not xml_str:
        xml_str = msg.display_content or ""
    if not xml_str:
        return ""
    try:
        root = ET.fromstring(xml_str)
        title = root.findtext(".//title", "")
        if title:
            return title
    except ET.ParseError:
        warn("db/content_extract.py:452 ET.ParseError", err=repr(__import__("sys").exc_info()[1]))
    import re
    title_match = re.search(r"<title>(.*?)</title>", xml_str, re.DOTALL)
    if title_match:
        return title_match.group(1).strip()
    return ""


def _decompress_xml(msg: WxMessage) -> str:
    if msg.compress_content:
        result = _decompress_bytes(msg.compress_content)
        if result:
            return result

    if msg.str_content:
        if isinstance(msg.str_content, bytes):
            result = _decompress_bytes(msg.str_content)
            if result:
                return result
            try:
                return msg.str_content.decode("utf-8", errors="replace")
            except Exception:
                warn("db/content_extract.py:474 Exception", err=repr(__import__("sys").exc_info()[1]))
                return ""
        return msg.str_content

    return ""


def _fallback_extract(xml_str: str) -> str:
    import re
    title_match = re.search(r"<title>(.*?)</title>", xml_str, re.DOTALL)
    if title_match:
        title = title_match.group(1).strip()
        # 2026-11-02：微信把标题包在 CDATA 里，直接返回会把
        # "<![CDATA[微信转账]]>" 原样当正文入库（account.db 里已有 22 条这种）。
        cdata = re.fullmatch(r"<!\[CDATA\[(.*?)\]\]>", title, re.DOTALL)
        if cdata:
            title = cdata.group(1).strip()
        return title
    return ""
