from __future__ import annotations

import binascii

import asyncio
import base64
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

import structlog
from safelog import warn

log = structlog.get_logger()

_EMOJI_DESC_CACHE_PATH = Path("data/emoji_desc_cache.json")
_EMOJI_IMAGE_CACHE_DIR = Path("data/image_cache")
_EMOJI_RAW_CACHE_DIR = Path("data/emoji_cache")
# The md5 is hex and hex has two spellings, so the character class accepts
# both.  With ``[0-9a-f]`` alone an upper-case md5 did not match the pattern at
# all, so ``replace_emoji_descriptions`` returned the raw hash without ever
# consulting ``get_emoji_description`` -- the normalisation there never got a
# chance to run.  The registry joins on md5, so a case mismatch silently lost
# the owner's own wording for that sticker.
_MD5_PATTERN = re.compile(r"\[表情包:([0-9a-fA-F]{32})\]")
# 任意 token（含 base64 文字型贴纸），替换时统一处理。
_TOKEN_PATTERN = re.compile(r"\[表情包:([^\]]+)\]")
# 文字型贴纸的 token 是 base64 的 protobuf，里面带贴纸自己的文字：
#   ChcKB2RlZm1bHQSDOaDs+WQg+mlreWWtQ== → 想吃饭喵
# 2026-09-28 实测：近 2 天 73 个 base64 token 全部可解出文字，此前整串 base64
# 直接进了 prompt（模型只能看到乱码）。
_CJK_RUN_RE = re.compile(rb"(?:[\xe4-\xe9][\x80-\xbf]{2})+")

_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
    (b"RIFF", "webp", "image/webp"),
)


def _sniff_image(data: bytes) -> tuple[str, str]:
    """Return (extension, mime) for known image bytes, else ('', '')."""
    for magic, ext, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            if ext == "webp" and b"WEBP" not in data[:16]:
                continue
            return ext, mime
    return "", ""

_MAX_CONCURRENT_RECOGNITIONS = 3
# 2026-10-27：表情包描述用多少 token。
# 原来写 60 —— 实测 deepseek-flash 把 60 个 token 全花在 reasoning 上
# （llm_call 日志 completion=60 reasoning=60），finish_reason='length'、text 为空，
# 于是「描述无效」→ 缓存永远为空 → 她引用表情包时只剩一串 md5。
_EMOJI_SYSTEM = (
    "你是表情包描述专家。用2-8个中文词精准描述这个表情包，"
    "要同时体现外观动作和情感，如「捂脸大笑」「无奈叹气」「竖大拇指」"
    "「猫咪惊讶」「委屈巴巴」「傲娇哼哼」。只输出描述，不要其他内容。"
)
# 实测（2026-10-27，deepseek-flash，同一个 gif）：
#   max_tokens=60 / 300 / 600 → finish_reason=length，reasoning_tokens 吃满，text 为空；
#   max_tokens=2000          → finish_reason=stop，reasoning 只用 204，text='蓝帽黄脸钻进被窝'。
# 结论：思考型模型的预算必须给足，否则它永远停在「思考」里出不来。别省这个钱。
_EMOJI_MAX_TOKENS = 2000
_EMOJI_RETRY_TOKENS = 2400
_RECOGNITION_INTERVAL_SECONDS = 0.5

# Rejections applied to whatever the vision model returns, see
# ``clean_description``: sentence punctuation means it wrote prose, and the
# listed words mean it answered that it could not describe the image at all.
_DESC_SENTENCE_CHARS = "。，！？、,.!?"
_DESC_REFUSALS = ("无法", "不能", "抱歉", "没有可识别", "没有可")

_cache: dict[str, str] = {}
_cache_loaded = False
_semaphore: Optional[asyncio.Semaphore] = None
_in_flight: set[str] = set()

# md5 -> the owner's own name for that picture, loaded lazily from the sticker
# registry.  Keyed the same way as ``_cache`` because both answer "what is this
# sticker?", and merged on read in ``get_emoji_description``.
_owner_index: dict[str, str] = {}
_owner_index_loaded = False


def _load_owner_index() -> dict[str, str]:
    """The owner's names for stickers, from the send-side registry.

    Two systems describe the same picture and they never talked to each other.
    The send side holds the owner's wording ("太菜了"); this module held only
    what the vision model guessed ("奖杯无奈叹气").  The owner asked that their
    own wording win whenever the sticker is one they set up, so it is consulted
    first and the vision description is only a fallback.

    Failure is non-fatal: if the registry cannot be read the cache still works,
    it just loses the owner's wording for this process.
    """
    global _owner_index, _owner_index_loaded
    if _owner_index_loaded:
        return _owner_index
    _owner_index_loaded = True
    try:
        from db import sticker_store

        _owner_index = sticker_store.owner_description_index("wechat", None)
    except Exception as e:
        log.warning("emoji_owner_index_load_failed", error=str(e)[:160])
        _owner_index = {}
    return _owner_index


def _reset_owner_index() -> None:
    """Drop the memoized owner index (sticker registry edited at runtime)."""
    global _owner_index, _owner_index_loaded
    _owner_index = {}
    _owner_index_loaded = False


def _load_cache() -> dict[str, str]:
    global _cache, _cache_loaded
    if _cache_loaded:
        return _cache
    try:
        if _EMOJI_DESC_CACHE_PATH.exists():
            with open(_EMOJI_DESC_CACHE_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for k, v in raw.items():
                if isinstance(v, dict):
                    _cache[k] = v.get("desc", "")
                elif isinstance(v, str):
                    _cache[k] = v
                else:
                    _cache[k] = str(v)
        else:
            _cache = {}
    except Exception as e:
        log.warning("emoji_desc_cache_load_failed", error=str(e))
        _cache = {}
    _cache_loaded = True
    return _cache


def _save_cache(data: dict[str, str]) -> None:
    global _cache
    _cache = data
    try:
        _EMOJI_DESC_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_EMOJI_DESC_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("emoji_desc_cache_save_failed", error=str(e))


def get_emoji_description(md5_prefix: str) -> Optional[str]:
    """The description to show the model for a sticker.

    Order matters and is deliberate: a sticker the owner set up carries their
    own wording, and the owner asked for it to be used verbatim whenever it is
    recognised.  Only stickers the owner never named fall back to the vision
    model's guess.
    """
    # md5s are hex, so normalise case on both lookups: an inbound message and
    # the stored template filename can differ in case while naming the same
    # sticker.  Normalising only one side made a sticker the owner had named
    # silently fall back to the vision guess.
    key = str(md5_prefix or "").strip().lower()
    owner = _load_owner_index().get(key)
    if owner:
        return owner
    cache = _load_cache()
    return cache.get(key) or cache.get(md5_prefix)


def update_emoji_description(md5_prefix: str, new_desc: str) -> bool:
    if not new_desc or len(new_desc) > 30:
        return False
    cache = _load_cache()
    old_desc = cache.get(md5_prefix, "")
    cache[md5_prefix] = new_desc
    _save_cache(cache)
    if old_desc != new_desc:
        log.info("emoji_desc_corrected", md5=md5_prefix, old=old_desc, new=new_desc)
    return True


def decode_sticker_token(token: str) -> str:
    """把"文字型"表情包 token（base64 protobuf）解成文字；解不出返回 ""。

    只取 UTF-8 中文段——protobuf 里还有 ``default`` 之类的英文元数据，不能当内容；
    32 位 md5（图片型贴纸）走描述表，不走这里。
    """
    raw_token = str(token or "").strip()
    if not raw_token or len(raw_token) < 8 or len(raw_token) > 200:
        return ""
    if re.fullmatch(r"[0-9a-fA-F]{32}", raw_token):
        return ""
    if not raw_token.isascii():
        return ""
    try:
        data = base64.b64decode(raw_token, validate=True)
    except (binascii.Error, ValueError):
        warn("emoji_token_not_base64", n=len(raw_token), head=raw_token[:6])
        return ""
    if not data or len(data) > 200:
        return ""
    chunks: list[str] = []
    for seg in _CJK_RUN_RE.findall(data):
        try:
            text = seg.decode("utf-8")
        except Exception:
            warn("core/preprocess/emoji_recognition.py:193 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
        if text:
            chunks.append(text)
    return " ".join(chunks).strip()[:30]


def replace_emoji_descriptions(text: str) -> str:
    """Swap ``[表情包:<md5>]`` for a human-readable description.

    Uses ``get_emoji_description``, so an owner-named sticker always reads back
    as the owner's own wording rather than the vision model's guess.
    """
    if not text or "[表情包:" not in text:
        return text

    def _replace(m: re.Match) -> str:
        token = m.group(1)
        if re.fullmatch(r"[0-9a-fA-F]{32}", token):
            desc = get_emoji_description(token)
            return f"[表情包:{desc}]" if desc else m.group(0)
        decoded = decode_sticker_token(token)
        if decoded:
            return f"[表情包:{decoded}]"
        return m.group(0)

    return _TOKEN_PATTERN.sub(_replace, text)


def _find_emoji_image(md5_prefix: str) -> Optional[Path]:
    for cache_dir in (_EMOJI_IMAGE_CACHE_DIR, _EMOJI_RAW_CACHE_DIR):
        if not cache_dir.exists():
            continue
        for f in cache_dir.iterdir():
            if f.stem != md5_prefix or f.stat().st_size <= 0:
                continue
            if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                return f
            # CDN prefetched stickers are often saved as "<md5>.dat" but the
            # payload is already a GIF/PNG/JPEG.  Sniff instead of trusting
            # the extension.
            try:
                if _sniff_image(f.read_bytes()):
                    return f
            except Exception:
                warn("core/preprocess/emoji_recognition.py:237 Exception", err=repr(__import__("sys").exc_info()[1]))
                continue
    return None


def find_emoji_image_path(md5_prefix: str) -> str:
    """Local image file for a sticker md5, or "" when it is not cached.

    The lookup half of :func:`recognize_emoji`, exposed for callers that need
    the *file* instead of the description.  A chat's media-registry row for an
    inbound sticker can still be ``pending`` while the shared sticker blob is
    already in the emoji cache -- the registry tracks the message's own copy,
    not the sticker store -- so "can this sticker be looked at?" has to ask
    here as well, not only the chat's media rows.
    """
    path = _find_emoji_image(str(md5_prefix or "").strip().lower())
    return str(path) if path else ""



def _vision_text(raw: Any) -> str:
    """从视觉调用的返回值里取「模型真正写出来的那几个词」。

    字符串就是答案；ChatResult 里 text 常为空，观察在 reasoning_blocks 中
    （2026-10-26 起 media.py 的 _pick_vision_text 就是这个逻辑，这里复用）。
    """
    if isinstance(raw, str):
        return raw.strip()
    try:
        from core.conversation.media import _pick_vision_text

        return _pick_vision_text(raw)
    except Exception:  # noqa: BLE001 - 取不到就退最朴素读法
        return str(getattr(raw, "text", "") or "").strip()


async def recognize_emoji(md5_prefix: str) -> Optional[str]:
    existing = get_emoji_description(md5_prefix)
    if existing:
        return existing

    img_path = _find_emoji_image(md5_prefix)
    if not img_path:
        log.debug("emoji_image_not_found", md5=md5_prefix)
        return None

    try:
        img_data = img_path.read_bytes()
        _, mime = _sniff_image(img_data)
        mime = mime or "image/jpeg"
        b64 = base64.b64encode(img_data).decode("ascii")
        image_arg = f"data:{mime};base64,{b64}"
    except Exception as e:
        log.warning("emoji_read_failed", md5=md5_prefix, error=str(e))
        return None

    try:
        from core.bootstrap import get_llm
        llm = get_llm()
        raw = await llm.chat_vision(
            system=_EMOJI_SYSTEM,
            user="描述这个表情包",
            images=[image_arg],
            agent_name="emoji_recog",
            temperature=0.2,
            max_tokens=_EMOJI_MAX_TOKENS,
            return_result=True,
        )
        desc = clean_description(_vision_text(raw))
        if not desc:
            # 思考型模型可能把预算吃光：放宽预算、温度归零再问一次。
            raw = await llm.chat_vision(
                system=_EMOJI_SYSTEM,
                user="描述这个表情包",
                images=[image_arg],
                agent_name="emoji_recog",
                temperature=0.0,
                max_tokens=_EMOJI_RETRY_TOKENS,
                return_result=True,
            )
            desc = clean_description(_vision_text(raw))
        if desc:
            cache = _load_cache()
            cache[md5_prefix] = desc
            _save_cache(cache)
            log.info("emoji_recognized", md5=md5_prefix, desc=desc)
            return desc
        log.warning("emoji_desc_invalid", md5=md5_prefix,
                    finish=str(getattr(raw, "finish_reason", "") or "")[:40],
                    seen=_vision_text(raw)[:60])
        return None
    except Exception as e:
        log.warning("emoji_recognition_failed", md5=md5_prefix, error=str(e))
        return None


def clean_description(raw: str) -> str:
    """Return a usable short phrase, or "" when the model did not describe.

    A description is a phrase ("捂脸大笑"), never a sentence.  The vision model
    answers a refusal instead when it cannot see a cartoon in the image, and one
    such refusal -- "这是一张纯黑色的图片，没有可识别的表情包内容，无法进行描述。"
    -- is exactly 30 characters, so a length bound alone let it through.  It was
    cached and then written into history, where it read as a description.
    """
    desc = str(raw or "").strip().strip('"').strip("'").strip()
    if not desc or len(desc) > 30:
        return ""
    if any(ch in desc for ch in _DESC_SENTENCE_CHARS):
        return ""
    if any(word in desc for word in _DESC_REFUSALS):
        return ""
    return desc


async def ensure_emoji_description(md5_prefix: str) -> str:
    """Return a cached sticker description, recognizing it on demand if needed."""
    md5_prefix = str(md5_prefix or "").strip().lower()
    if not md5_prefix:
        return ""
    existing = get_emoji_description(md5_prefix)
    if existing:
        return existing
    try:
        desc = await recognize_emoji(md5_prefix)
    except Exception as e:
        log.warning("emoji_ensure_failed", md5=md5_prefix, error=str(e)[:160])
        return ""
    return desc or ""


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT_RECOGNITIONS)
    return _semaphore


def schedule_emoji_recognition(md5_prefix: str) -> None:
    if get_emoji_description(md5_prefix):
        return
    if md5_prefix in _in_flight:
        return
    _in_flight.add(md5_prefix)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_recognize_and_update_db(md5_prefix))
        return
    except RuntimeError:
        # 2026-10-27：入站同步跑在没有事件循环的线程里（db/sync_service 的 exec 线程），
        # 以前这里直接 `_in_flight.discard` 放弃 —— 于是"她自己发的表情包"永远攒不到
        # 描述（silent_warnings 里 `...emoji_recognition.py:357 RuntimeError` 47 次），
        # 她引用它时只能看见一串编号。没有 loop 就自己起一个 daemon 线程跑完。
        warn("core/preprocess/emoji_recognition.py:357 RuntimeError",
             err=repr(__import__("sys").exc_info()[1]))
    try:
        threading.Thread(target=_run_recognize_threaded, args=(md5_prefix,),
                         name="emoji-recog", daemon=True).start()
    except Exception:  # noqa: BLE001 - 起不来也不能影响入站
        warn("core/preprocess/emoji_recognition.py:363 ThreadError",
             err=repr(__import__("sys").exc_info()[1]))
        _in_flight.discard(md5_prefix)


def _run_recognize_threaded(md5_prefix: str) -> None:
    """没有事件循环时的替身：自己开一个 loop 把识别跑完。"""
    try:
        asyncio.run(_recognize_and_update_db(md5_prefix))
    except Exception:  # noqa: BLE001 - 后台任务永不影响主流程
        warn("core/preprocess/emoji_recognition.py:_run_recognize_threaded Exception",
             err=repr(__import__("sys").exc_info()[1]))
    finally:
        _in_flight.discard(md5_prefix)


def _candidate_databases() -> list[str]:
    """Every database that may hold message rows for this account.

    Patching only the *active* repository left the legacy database's backlog
    untouched (326 rows at the time of writing), so a description could be
    replaced in one place while the same emoji still read as a raw placeholder
    when she searched history.  Enumerate rather than assume.
    """
    paths: list[str] = []
    try:
        from core.bootstrap import get_legacy_db_path
        legacy = get_legacy_db_path()
        if legacy:
            paths.append(str(legacy))
    except Exception:  # noqa: BLE001
        warn("core/preprocess/emoji_recognition.py:375 Exception", err=repr(__import__("sys").exc_info()[1]))
    try:
        from core.config.settings import config
        for account in config.all_account_configs():
            db_path = str(getattr(account, "db_path", "") or "")
            if db_path:
                paths.append(db_path)
    except Exception:  # noqa: BLE001
        warn("core/preprocess/emoji_recognition.py:383 Exception", err=repr(__import__("sys").exc_info()[1]))
    try:
        from core.terminal.store_port import get_repo
        url = str(getattr(get_repo().engine, "url", ""))
        prefix = "sqlite:///"
        if url.startswith(prefix):
            paths.append(url[len(prefix):])
    except Exception:  # noqa: BLE001
        warn("core/preprocess/emoji_recognition.py:391 Exception", err=repr(__import__("sys").exc_info()[1]))

    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen or not os.path.exists(path):
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _patch_placeholders(db_path: str, md5_prefix: str, desc: str) -> int:
    """Replace the placeholder in one database.  Returns rows changed."""
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.orm import Session

    from db.models import Message

    # A plain engine, so give it the same busy timeout the runtime uses: this
    # runs while the scheduler itself holds these files open.
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"timeout": 30},
    )
    try:
        # The meta database is in the candidate set but stores conversations,
        # not messages; skipping it here keeps a harmless absence from looking
        # like a failure on every single emoji.
        if not inspect(engine).has_table(Message.__tablename__):
            return 0
        with Session(engine) as session:
            rows = session.query(Message).filter(
                Message.content.like(f"%[表情包:{md5_prefix}%")
            ).all()
            changed = 0
            for row in rows:
                old = row.content or ""
                new = old.replace(f"[表情包:{md5_prefix}]", f"[表情包:{desc}]")
                if new != old:
                    row.content = new
                    changed += 1
            session.commit()
            return changed
    finally:
        engine.dispose()


async def _recognize_and_update_db(md5_prefix: str) -> None:
    sem = _get_semaphore()
    async with sem:
        await asyncio.sleep(_RECOGNITION_INTERVAL_SECONDS)
        desc = await recognize_emoji(md5_prefix)
    _in_flight.discard(md5_prefix)
    if not desc:
        return
    try:
        # This used to import a module-level ``get_session`` from ``db.models``
        # that never existed, so the description was computed and then thrown
        # away on every single emoji: the placeholder never got replaced and
        # the log never once recorded a success.
        total = 0
        touched: list[str] = []
        for db_path in _candidate_databases():
            try:
                changed = _patch_placeholders(db_path, md5_prefix, desc)
            except Exception as exc:  # noqa: BLE001 - one bad db must not stop the rest
                log.warning("emoji_db_patch_skipped", db=db_path, error=str(exc)[:120])
                continue
            if changed:
                total += changed
                touched.append(f"{os.path.basename(os.path.dirname(db_path)) or '.'}"
                               f"/{os.path.basename(db_path)}={changed}")
        if total:
            log.info("emoji_db_updated", md5=md5_prefix, desc=desc,
                     count=total, databases=", ".join(touched))
    except Exception as e:
        log.warning("emoji_db_update_failed", md5=md5_prefix, error=str(e))
