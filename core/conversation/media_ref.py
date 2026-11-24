"""「引用一条媒体」的定位串 → 看得见的描述（2026-10-27）。

事故：Elaina 引用鲸鲸自己发的表情包问「你能看见这个表情包吗」，鲸鲸回「我这边只看到
一串编号」。微信把被引用的媒体写成定位串

    [引用 Elaina: wxid_seed01:1793059376769:1:9b1203029ed1f0f8cbbf7178bf846bd2::0]

而 loop/ports.py、loop/snapshot.py、core/context/chat_window.py 三处渲染器都只认字面
量 ``[图片]``，于是整串原样进了 prompt —— 那张图 12 秒前就被视觉模型描述过、描述就在
``message_media.description`` 里，只是没有人去查。

本模块是唯一认这种串的地方。定位串按冒号分段：
    0 会话 key（私聊即对方 wxid）      1 原消息时间戳（毫秒，本地时区）
    2 媒体序号（微信自己用，恒为 1）    3 媒体 md5（= sticker_id，表情包也是它）
描述来源依次是：调用方给的现成描述表 → 表情包命名表/识别缓存 → 媒体库那一行 →
本地文件 + 视觉模型（可选、带预算）。拿不到就**原样返回**，绝不编造。
"""
from __future__ import annotations

import os
import sqlite3

from core.infra import skips
import threading
import time
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

# `[引用 作者: chat:ts_ms:seq:md5::0]`
LOCATOR_RE = re.compile(
    r"\[引用\s*([^:\]\[]{1,24})[:：]\s*([^\s:\]]{2,64}):(\d{10,16}):(\d{1,3})"
    r":([0-9a-fA-F]{32})(?:::0)?\]"
)
# 老形态：`[引用 P6: [图片]]`（微信对普通图片用的是这个，被引用图的行上带着 media_path）
PLAIN_QUOTED_IMG_RE = re.compile(r"(\[引用 [^\]]{1,24}: )\[图片\]")
# 2026-10-27: 入库时从被引用消息的 <appattach> 里捡回的 md5（以前一律塌成「[文件]」）。
# md5 不该进模型的眼睛，渲染时换成描述；查不到就退回裸占位符。
MD5_TAG_RE = re.compile(r"\[(图片|表情包)[:：]\s*([0-9a-fA-F]{32})\]")

NOPIC = "（画面内容难以辨认）"
# 描述里出现这些词说明它不是"图里有什么"，而是任务书/草稿/拒绝 —— 一律不采信。
_BAD_DESC = (
    "30字以内", "30 字以内", "30字内", "禁止项", "用户想要", "目标：", "目标:",
    "字数", "最终一句话", "草稿", "一句话概括", "总结：", "无法", "不能", "抱歉",
)

_MAX_DESC = 40
_STICKER_DESC = 30
_SUFFIXES = ("gif", "png", "jpg", "jpeg", "webp", "bmp")
_SEARCH_DIRS = ("data/image_cache", "data/emoji", "data/media", "data/stickers")

_LOCK = threading.Lock()
_MEM: dict[str, str] = {}
_VER = {"n": 0}
_TRIED: dict[str, int] = {}
_NEW = {"n": 0}
_PREF = {"n": 0}
# 每拍最多现问一次视觉模型：渲染在 tick 里同步跑，不能每张图都等 8 秒。
MAX_NEW_CALLS_PER_BUILD = 1
MAX_PREFETCH_PER_BUILD = 2


@dataclass(frozen=True)
class MediaLocator:
    """一条引用媒体定位串。``start`` / ``end`` 是它在原串里的位置。"""

    raw: str
    start: int
    end: int
    author: str
    chat_key: str
    ts_ms: int
    seq: int
    md5: str

    @property
    def moment(self) -> datetime | None:
        try:
            return datetime.fromtimestamp(self.ts_ms / 1000.0)
        except (OverflowError, OSError, ValueError):
            return None


def parse_locators(text: str) -> list[MediaLocator]:
    """把文本里所有引用媒体定位串解出来（顺序保持原样）。"""
    body = str(text or "")
    out: list[MediaLocator] = []
    for m in LOCATOR_RE.finditer(body):
        out.append(MediaLocator(
            raw=m.group(0),
            start=m.start(),
            end=m.end(),
            author=m.group(1).strip(),
            chat_key=m.group(2).strip(),
            ts_ms=int(m.group(3)),
            seq=int(m.group(4)),
            md5=m.group(5).lower(),
        ))
    return out


def has_locator(text: str) -> bool:
    """这段文本里有没有「引用的是一条媒体」。渲染层用它决定值不值得查库。"""
    return bool(LOCATOR_RE.search(str(text or "")))


def reset_budget() -> None:
    """每一拍（一次快照构建）开始前调一次：允许再新问一次视觉模型。"""
    with _LOCK:
        _NEW["n"] = 0
        _PREF["n"] = 0


def _flat(text: Any) -> str:
    return " ".join(str(text or "").split())


def _usable(desc: Any) -> str:
    """一段描述能不能当"她看见了什么"用。"""
    text = _flat(desc)
    if not text or text == NOPIC:
        return ""
    if any(bad in text for bad in _BAD_DESC):
        return ""
    return text


def _remember(md5: str, desc: str) -> str:
    text = _usable(desc)
    if not md5 or not text:
        return ""
    with _LOCK:
        _MEM[md5] = text
        _VER["n"] += 1
    return text


def desc_version() -> int:
    """本进程里「又认出/查到一张图」的次数。

    loop 只在世界指纹变了才重搭快照（loop/run.py:233），而描述常常是后台预取
    补上的——补上那一刻没有任何消息变化。把这个计数器掺进指纹里，她才看得见
    刚补好的那张图，而不是等下一句消息顺带捎过去。
    """
    with _LOCK:
        return int(_VER["n"])


def _cached(md5: str) -> str:
    with _LOCK:
        return _MEM.get(md5, "")


def _claim_prefetch_slot() -> bool:
    """后台预问也有上限：一屏十几张历史图不该同时开十几个视觉请求。"""
    with _LOCK:
        if _PREF["n"] >= MAX_PREFETCH_PER_BUILD:
            return False
        _PREF["n"] += 1
        return True


def _claim_vision_slot() -> bool:
    with _LOCK:
        if _NEW["n"] >= MAX_NEW_CALLS_PER_BUILD:
            return False
        _NEW["n"] += 1
        return True
    # 注：_TRIED 只用来统计"这张图问过几次"，真正限次在 media.ensure_image_description


def _from_descs(descs: Mapping[str, str] | None, md5: str) -> str:
    """在调用方给的 `{文件名: 描述}` 表里找这张图（按 md5 试各种后缀）。"""
    if not descs or not md5:
        return ""
    for name in [md5] + [f"{md5}.{s}" for s in _SUFFIXES]:
        desc = _usable(descs.get(name))
        if desc:
            return desc
    return ""


_DBS = {"at": 0.0, "paths": ()}
_DBS_TTL_S = 300.0


def _account_dbs() -> tuple:
    """这一台机器上可能存着 message_media 的库（账号库 / 遗留库）。"""
    now = time.time()
    if _DBS["paths"] and now - float(_DBS["at"]) < _DBS_TTL_S:
        return _DBS["paths"]
    try:
        from core.preprocess.emoji_recognition import _candidate_databases

        paths = tuple(str(p) for p in _candidate_databases() if p)
    except Exception:  # noqa: BLE001 - 列不出来就当没有
        paths = ()
    _DBS.update({"at": now, "paths": paths})
    return paths


def _from_account_db(md5: str) -> str:
    """账号库 `message_media` 里这张媒体的描述。

    2026-10-27：入站那一步写的就是这张表；loop / 窗口渲染手里没有可写 engine
    （`db.write_guard` 会拦），但**读**它是安全的 —— 直接只读连 sqlite，零 API、零延迟。
    她引用表情包时缺的就是这一步：md5 → `<md5>.gif` → description。
    """
    key = str(md5 or "").strip().lower()
    if len(key) != 32:
        return ""
    like = "%" + key + "%"
    for db in _account_dbs():
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=1.0)
            try:
                rows = con.execute(
                    "select description from message_media "
                    "where description is not null and trim(description) <> '' "
                    "and local_path is not null and lower(local_path) like ? "
                    "order by id desc limit 5", (like,)).fetchall()
            finally:
                con.close()
        except Exception as exc:  # 表不在/库锁着都只是「暂时没描述」，但要看得出是哪一种
            skips.skip("media_ref._from_account_db", "absent",
                       "%s: %s" % (type(exc).__name__, exc))
            continue
        for row in rows:
            desc = _usable(row[0] if not isinstance(row, dict) else row.get("description"))
            if desc:
                return desc
    return ""


def _from_emoji_cache(md5: str) -> str:
    """表情包的命名表 + 识别缓存（跨进程的 json，loop 也能读）。"""
    try:
        from core.preprocess.emoji_recognition import get_emoji_description

        return _usable(get_emoji_description(md5))
    except Exception:  # noqa: BLE001 - 渲染不因为缓存坏了而失败
        return ""


def _local_paths(md5: str, path: str = "") -> list[str]:
    """这张图在本地可能的文件（先看行上给的路径，再看表情包缓存，最后扫目录）。"""
    out: list[str] = []

    def _add(p: Any) -> None:
        text = str(p or "").strip()
        if not text:
            return
        if not os.path.isabs(text):
            text = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), text)
        if os.path.isfile(text) and text not in out:
            out.append(text)

    _add(path)
    if md5:
        try:
            from core.preprocess.emoji_recognition import find_emoji_image_path

            _add(find_emoji_image_path(md5))
        except Exception:  # noqa: BLE001
            pass
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for sub in _SEARCH_DIRS:
            base = os.path.join(root, sub.replace("/", os.sep))
            if not os.path.isdir(base):
                continue
            for suffix in _SUFFIXES:
                _add(os.path.join(base, f"{md5}.{suffix}"))
    return out


def _from_media_row(*, engine=None, path: str = "", chat_id: str = "",
                    created_at=None) -> str:
    """媒体库那一行（入站时视觉模型写下的描述）。没有 engine 就跳过。"""
    if engine is None or (not path and not (chat_id and created_at is not None)):
        return ""
    try:
        from core.conversation.media_registry import lookup_description

        _, desc = lookup_description(
            engine, path=path, chat_id=chat_id, created_at=created_at,
        )
        return _usable(desc)
    except Exception:  # noqa: BLE001
        return ""


def _ask_vision(path: str, *, mode: str, budget_s: float) -> str:
    """现问一次视觉模型。``mode`` = peek / prefetch / wait。"""
    if mode not in ("prefetch", "wait") or not path:
        return ""
    try:
        from core.conversation import media as _media

        if mode == "prefetch":
            if _claim_prefetch_slot():
                _media.prefetch_image_description(path, budget_s=budget_s)
            return ""
        if not _media.describe_on_turn_enabled():
            return ""
        if not _claim_vision_slot():
            if _claim_prefetch_slot():
                _media.prefetch_image_description(path, budget_s=budget_s)
            return ""
        return _usable(_media.ensure_image_description(path, budget_s=budget_s))
    except Exception:  # noqa: BLE001 - 视觉链路坏了就退回"没看见"，不抛
        return ""


def describe_md5(md5: str, *, descs: Mapping[str, str] | None = None,
                 path: str = "", chat_id: str = "", created_at=None,
                 engine=None, mode: str = "peek", budget_s: float = 8.0) -> str:
    """这张图她看见了什么；没有描述就返回 ""。

    ``mode``：
      - ``peek``（默认）：只查已有描述（内存 / 描述表 / 表情包缓存 / 媒体库 / 本地缓存）；
      - ``prefetch``：查不到就让后台去问，本拍不等；
      - ``wait``：查不到就现问一次（有硬超时，且每拍最多一张）。
    """
    key = str(md5 or "").strip().lower()
    if not key:
        return ""
    hit = _cached(key)
    if hit:
        return hit
    desc = _from_descs(descs, key) or _from_account_db(key) or _from_emoji_cache(key)
    if not desc:
        desc = _from_media_row(engine=engine, path=path, chat_id=chat_id,
                               created_at=created_at)
    if not desc:
        for cand in _local_paths(key, path):
            desc = _from_media_row(engine=engine, path=cand)
            if desc:
                break
    if not desc and path:
        desc = _from_descs(descs, os.path.splitext(os.path.basename(path))[0].lower())
    if desc:
        return _remember(key, desc)
    for cand in _local_paths(key, path):
        desc = _ask_vision(cand, mode=mode, budget_s=budget_s)
        if desc:
            return _remember(key, desc)
    return ""


def describe_sticker(md5: str, *, descs: Mapping[str, str] | None = None,
                     path: str = "", engine=None, mode: str = "peek",
                     budget_s: float = 8.0) -> str:
    """表情包 / 贴纸的描述（短一些，最多 30 字）。"""
    return describe_md5(md5, descs=descs, path=path, engine=engine,
                        mode=mode, budget_s=budget_s)[:_STICKER_DESC]


def is_image_path(path: str) -> bool:
    # 这条本地文件其实是张图/动图吗（微信常把 gif 当「文件」发出来）。
    ext = os.path.splitext(os.path.basename(str(path or "")))[1].lstrip(".").lower()
    return ext in _SUFFIXES


def render_md5_tags(text: str, *, descs: Mapping[str, str] | None = None,
                    mode: str = "peek", budget_s: float = 8.0,
                    max_chars: int = _MAX_DESC) -> str:
    # `[图片:9b12...]` / `[表情包:9b12...]` -> `[图片: 描述]`；查不到退回裸占位符。
    # 2026-10-27: core/conversation/quote_text.py 以前把 <appattach> 整个塌成「[文件]」，
    # 名字/类型/md5 全丢 — 她引用一张动图时看见的就是「就一个字 文件」。
    body = str(text or "")
    if not body or not MD5_TAG_RE.search(body):
        return body
    for m in reversed(list(MD5_TAG_RE.finditer(body))):
        kind, md5 = m.group(1), m.group(2).lower()
        if kind == "表情包":
            desc = describe_sticker(md5, descs=descs, mode=mode, budget_s=budget_s)
        else:
            desc = describe_md5(md5, descs=descs, mode=mode,
                                budget_s=budget_s)[:max_chars]
        body = body[:m.start()] + ("[%s: %s]" % (kind, desc) if desc else "[%s]" % kind) + body[m.end():]
    return body

def render_media_locators(text: str, *, descs: Mapping[str, str] | None = None,
                          path: str = "", chat_id: str = "", created_at=None,
                          engine=None, mode: str = "peek", budget_s: float = 8.0,
                          max_chars: int = _MAX_DESC) -> str:
    """把引用媒体换成「[引用 作者: [图片: 描述]]」；查不到就原样留着。

    两种形态都认：定位串（表情包/语音等）和 ``[引用 X: [图片]]``（普通图片）。
    """
    body = str(text or "")
    if not body:
        return body
    body = render_md5_tags(body, descs=descs, mode=mode, budget_s=budget_s,
                           max_chars=max_chars)
    if "[引用" not in body:
        return body

    # 一、普通图片形态：描述就挂在这一行自己的 media_path 上
    if PLAIN_QUOTED_IMG_RE.search(body):
        stem = os.path.splitext(os.path.basename(str(path or "")))[0].lower()
        desc = ""
        if stem:
            desc = describe_md5(stem, descs=descs, path=path, chat_id=chat_id,
                                created_at=created_at, engine=engine, mode=mode,
                                budget_s=budget_s)
        if desc:
            body = PLAIN_QUOTED_IMG_RE.sub(
                lambda m: m.group(1) + "[图片: %s]" % desc[:max_chars], body)

    # 二、定位串形态：md5 在第 4 段
    locators = parse_locators(body)
    for loc in reversed(locators):
        desc = describe_md5(loc.md5, descs=descs, chat_id=loc.chat_key or chat_id,
                            created_at=loc.moment or created_at, engine=engine,
                            mode=mode, budget_s=budget_s)
        if not desc:
            continue
        body = body[:loc.start] + "[引用 %s: [图片: %s]]" % (
            loc.author, desc[:max_chars]) + body[loc.end:]
    return body
