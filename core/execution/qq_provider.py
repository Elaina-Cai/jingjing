"""QQ Provider（OneBot v11 / NapCat）。

数据通路
========
NapCat 以 OneBot v11 协议暴露两个通道：

* **正向 WebSocket**：我们连它（``ws://host:port``，可带 ``access_token``），接收事件；
* **HTTP API**：我们调它（``http://host:port/send_group_msg`` 等），发送消息。

本模块只依赖标准库 + ``websockets`` + ``httpx``（后两者已在 requirements）。
事件解析是纯函数 ``inbound_from_event``，网络发送走 ``_api_call``（测试可打桩）。

约定
====
* ``chat_id`` 用 ``group:<群号>`` / ``user:<QQ号>`` 前缀区分群/私聊
  （对应 ``QQMessage.to_inbound``；Provider.send 解析前缀决定 API）；
* 事件里 @ 到机器人（``self_id`` / 配置账号）时置
  ``InboundMessage.extra["mention_self"]=True``（路由的 @ 触发读它）；
* ``send_enabled`` 为 false 时拒绝发送（与 ``WECHAT_SEND_ENABLED`` 同一套纪律）。

配置（.env，二选一）
====================
legacy 槽 + QQ 连接变量（单账号，MVP 推荐）::

    PLATFORMS_QQ_ACCOUNT=123456789
    PLATFORMS_QQ_OWNER_ACCOUNTS=987654321
    QQ_WS_URL=ws://127.0.0.1:3001
    QQ_HTTP_URL=http://127.0.0.1:3000
    QQ_ACCESS_TOKEN=
    QQ_SEND_ENABLED=true

或写进 ``PLATFORMS_CONFIGS_JSON`` 的 extra 字段（多账号用）::

    [{"platform":"qq","account_id":"123456789","role":"yinyue",
      "send_enabled":true,"ws_url":"ws://127.0.0.1:3001",
      "http_url":"http://127.0.0.1:3000","access_token":""}]
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import structlog

from core.execution.provider import (
    InboundMessage,
    MsgType,
    Platform,
    Provider,
    SendResult,
    register_provider_factory,
)
from core.ingestion.platform_messages import QQMessage
from safelog import warn

log = structlog.get_logger()

__all__ = ["QQProvider", "build_qq_provider", "inbound_from_event", "parse_chat_id"]

GROUP_PREFIX = "group:"
USER_PREFIX = "user:"
_WS_MAX_SIZE = 8 * 1024 * 1024
_DRAIN_LIMIT = 500

# 一条消息里出现多种段时，选一个「主类型」；reply 优先保引用，
# 媒体段仍然会填 media_* 字段（QQMessage.to_inbound 独立处理）。
_SEGMENT_PRIORITY = ("reply", "face", "image", "file", "video", "voice", "json")
_TEXT_HINTS = {"face": "[表情]", "image": "[图片]", "file": "[文件]",
               "video": "[视频]", "voice": "[语音]", "json": "[链接]"}


from core.infra import jsonio
from core.infra import liveness
from core.infra import egress


def parse_chat_id(chat_id: str) -> tuple[str, str]:
    """``group:123`` -> ``("group", "123")``；``user:456`` -> ``("private", "456")``。

    没有前缀时按私聊处理（保守，避免把 QQ 号当群号发错地方）。
    """
    text = str(chat_id or "")
    if text.startswith(GROUP_PREFIX):
        return "group", text[len(GROUP_PREFIX):]
    if text.startswith(USER_PREFIX):
        return "private", text[len(USER_PREFIX):]
    return "private", text


def inbound_from_event(
    event: Any,
    *,
    bot_id: str = "",
    resolve_name: Callable[[str], str] | None = None,
) -> InboundMessage | None:
    """OneBot v11 事件 -> InboundMessage；不是「收消息/拍一拍」时返回 None。"""
    if not isinstance(event, dict):
        return None
    post_type = str(event.get("post_type") or "")
    if post_type == "notice":
        return _inbound_from_notice(event, bot_id=bot_id, resolve_name=resolve_name)
    if post_type != "message":
        return None
    message_type = str(event.get("message_type") or "")
    if message_type not in ("group", "private"):
        return None

    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    self_id = str(event.get("self_id") or bot_id or "")
    user_id = str(event.get("user_id") or "")
    if not user_id or (self_id and user_id == self_id):
        return None  # 自己的消息/回显

    group_id = str(event.get("group_id") or "") if message_type == "group" else ""
    user_name = str(sender.get("card") or sender.get("nickname") or user_id)

    content_parts: list[str] = []
    at_users: list[dict[str, str]] = []
    flags: set[str] = set()
    face_id = media_url = media_file_id = json_data = ""
    reply_seq = 0
    reply_content = ""
    mention_self = False

    segments = event.get("message") if isinstance(event.get("message"), list) else []
    if not segments:
        raw = str(event.get("raw_message") or "")
        if raw:
            content_parts.append(raw)

    for segment in segments:
        if not isinstance(segment, dict):
            continue
        seg_type = str(segment.get("type") or "")
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        if seg_type == "text":
            content_parts.append(str(data.get("text") or ""))
        elif seg_type == "at":
            qq = str(data.get("qq") or "")
            nick = str(data.get("name") or "")
            at_users.append({"qq": qq, "nick": nick})
            content_parts.append(f"@{nick or qq}")
            if qq and self_id and qq == self_id:
                mention_self = True
        elif seg_type == "face":
            flags.add("face")
            face_id = str(data.get("id") or "")
            content_parts.append(_TEXT_HINTS["face"])
        elif seg_type == "image":
            flags.add("image")
            media_url = str(data.get("url") or "")
            media_file_id = str(data.get("file") or "")
            content_parts.append(_TEXT_HINTS["image"])
        elif seg_type == "reply":
            flags.add("reply")
            try:
                reply_seq = int(data.get("id") or 0)
            except (TypeError, ValueError):
                warn("core/execution/qq_provider.py:159 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
                reply_seq = 0
            reply_content = str(data.get("text") or "")
        elif seg_type in ("file", "video", "record"):
            key = "voice" if seg_type == "record" else seg_type
            flags.add(key)
            media_url = str(data.get("url") or "")
            media_file_id = str(data.get("file") or "")
            content_parts.append(_TEXT_HINTS[key])
        elif seg_type == "json":
            flags.add("json")
            json_data = str(data.get("data") or "")
            content_parts.append(_TEXT_HINTS["json"])
        else:
            # 未识别段：能拿到的 text 就保留，别整条丢
            extra_text = str(data.get("text") or "")
            if extra_text:
                content_parts.append(extra_text)

    raw_type = "text"
    for candidate in _SEGMENT_PRIORITY:
        if candidate in flags:
            raw_type = candidate
            break

    message = QQMessage(
        msg_id=str(event.get("message_id") or ""),
        seq=_as_int(event.get("message_seq") or event.get("message_id")),
        group_id=group_id,
        user_id=user_id,
        user_name=user_name,
        content="".join(content_parts).strip(),
        raw_type=raw_type,
        at_users=at_users,
        reply_seq=reply_seq,
        reply_content=reply_content,
        media_url=media_url,
        media_file_id=media_file_id,
        face_id=face_id,
        json_data=json_data,
        timestamp=_as_int(event.get("time")),
    )
    inbound = message.to_inbound()
    inbound.extra["mention_self"] = mention_self
    inbound.extra["self_id"] = self_id
    inbound.extra["raw_type"] = raw_type
    return inbound


def _display_name(user_id: str, resolve_name: Callable[[str], str] | None) -> str:
    """优先用联系人缓存里的名字，拿不到就退回 QQ 号（不猜）。"""
    value = str(user_id or "")
    if not value:
        return ""
    if resolve_name is not None:
        try:
            name = str(resolve_name(value) or "").strip()
        except Exception:  # noqa: BLE001
            warn("core/execution/qq_provider.py:216 Exception", err=repr(__import__("sys").exc_info()[1]))
            name = ""
        if name:
            return name
    return value


def _inbound_from_notice(
    event: dict,
    *,
    bot_id: str = "",
    resolve_name: Callable[[str], str] | None = None,
) -> InboundMessage | None:
    """OneBot notice 事件；目前只认拍一拍（poke），其余返回 None。

    内容形态对齐微信侧真线（``"X" 拍了拍 "Y"`` / 私聊 ``"X" 拍了拍我`` /
    自己拍的 ``我拍了拍 "Y"``），这样 ``chat_window`` 的拍一拍识别不用改。
    """
    if str(event.get("notice_type") or "") != "poke":
        return None
    user_id = str(event.get("user_id") or "")
    if not user_id:
        return None
    target_id = str(event.get("target_id") or "")
    group_id = str(event.get("group_id") or "")
    self_id = str(event.get("self_id") or bot_id or "")
    poker = _display_name(user_id, resolve_name)
    is_self = bool(self_id and user_id == self_id)
    if is_self:
        content = f'我拍了拍 "{_display_name(target_id, resolve_name)}"'
    elif not group_id and target_id and target_id == self_id:
        content = f'"{poker}" 拍了拍我'
    else:
        content = f'"{poker}" 拍了拍 "{_display_name(target_id, resolve_name)}"'

    extra: dict[str, Any] = {
        "poke": True,
        "target_id": target_id,
        "raw_type": "poke",
        "message_type": "group" if group_id else "private",
        "mention_self": bool(self_id and target_id == self_id),
    }
    if is_self:
        extra["is_self"] = True
    if group_id:
        extra["group_id"] = group_id
        chat_id = f"{GROUP_PREFIX}{group_id}"
    else:
        extra["user_id"] = user_id
        chat_id = f"{USER_PREFIX}{user_id}"
    stamp = _as_int(event.get("time"))
    return InboundMessage(
        platform=Platform.QQ,
        msg_id=f"poke-{group_id or user_id}-{stamp}-{target_id}",
        chat_id=chat_id,
        user_id=user_id,
        user_name=poker,
        content=content,
        msg_type=MsgType.PAT,
        is_group=bool(group_id),
        timestamp=datetime.fromtimestamp(stamp) if stamp else datetime.now(),
        extra=extra,
    )


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        warn("core/execution/qq_provider.py:284 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return 0


_MEDIA_MAX_BYTES = 12 * 1024 * 1024
_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
_SUFFIX_BY_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


def _guess_suffix(source: str) -> str:
    text = str(source or "").split("?", 1)[0].split("#", 1)[0].lower()
    for ext in _SUFFIXES:
        if text.endswith(ext):
            return ext
    return ".bin"


def _suffix_from_content_type(value: Any) -> str:
    return _SUFFIX_BY_TYPE.get(str(value or "").split(";")[0].strip().lower(), ".bin")


def _safe_name(value: Any) -> str:
    raw = str(value or "media")
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in raw)
    return cleaned[:80] or "media"


def _local_path(value: str) -> Path | None:
    text = str(value or "").strip()
    if not text.startswith("file://"):
        return Path(text) if text else None
    from urllib.parse import unquote, urlparse

    parsed = urlparse(text)
    path = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc not in ("", "localhost"):
        path = f"//{parsed.netloc}{path}"
    if len(path) > 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return Path(path) if path else None


# 「我还在听」的节拍：每 20 秒写一次，声明 60 秒内一定再写。
# 只证明「这条通道还在听」，不证明「有人说话」——后者夜里本来就静。
BEAT_MS = 60_000
BEAT_EVERY_S = 20.0


class QQProvider(Provider):
    """OneBot v11 Provider：WS 收事件，HTTP API 发消息。"""

    platform = Platform.QQ
    name = "qq"
    capabilities = frozenset({"text", "image"})

    def __init__(
        self,
        *,
        account_id: str = "",
        ws_url: str = "",
        http_url: str = "",
        access_token: str = "",
        send_enabled: bool = False,
        media_dir: str = "",
        send_min_interval: float = 1.5,
    ) -> None:
        self._account_id = str(account_id or "")
        self._ws_url = str(ws_url or "")
        self._http_url = str(http_url or "").rstrip("/")
        self._access_token = str(access_token or "")
        self._send_enabled = bool(send_enabled)
        self._media_dir = str(media_dir or "")
        self._send_min_interval = max(0.0, float(send_min_interval or 0.0))
        self._queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._member_names: dict[str, str] = {}
        self._last_directory_sync = 0.0
        self._send_lock = asyncio.Lock()
        self._last_send_at = 0.0
        self._client: Any = None
        self._connected = False
        self._closed = False

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        if not self._ws_url:
            log.warning("qq_provider_no_ws_url", account_id=self._account_id)
            return False
        if self._task is not None and not self._task.done():
            self._connected = True
            return True
        self._closed = False
        self._task = asyncio.create_task(
            self._ws_loop(), name=f"qq-ws-{self._account_id or 'default'}"
        )
        self._connected = True
        log.info("qq_provider_connected", account_id=self._account_id, ws=self._ws_url)
        beat = getattr(self, "_beat_task", None)
        if beat is None or beat.done():
            self._beat_task = asyncio.create_task(
                self._beat_loop(), name=f"qq-beat-{self._account_id or 'default'}"
            )
        return True

    async def disconnect(self) -> None:
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        if self._client is not None:
            with suppress(Exception):
                await self._client.aclose()
            self._client = None
        self._connected = False

    async def _ws_loop(self) -> None:
        url = self._ws_url
        if self._access_token and "access_token=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={self._access_token}"
        # 2026-10-27：NapCat 没起来时这里曾是「每 30 秒一条日志」，一天 2800+ 条。
        # 现在退避拉到 5 分钟，失败日志只在前 2 次和每 10 次各记一条。
        backoff = 2.0
        fails = 0
        while not self._closed:
            try:
                async with _ws_connect(url, max_size=_WS_MAX_SIZE,
                                       ping_interval=20, ping_timeout=20) as ws:
                    backoff = 2.0
                    fails = 0
                    log.info("qq_ws_connected", account_id=self._account_id)
                    async for raw in ws:
                        try:
                            event = json.loads(raw)
                        except (TypeError, ValueError):
                            warn("core/execution/qq_provider.py:418 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
                            continue
                        log.info("qq_ws_event", account_id=self._account_id, post_type=str(event.get("post_type") or ""), message_type=str(event.get("message_type") or ""))
                        inbound = inbound_from_event(
                            event, bot_id=self._account_id,
                            resolve_name=self._resolve_name,
                        )
                        if inbound is None:
                            log.info("qq_ws_event_skipped", account_id=self._account_id, post_type=str(event.get("post_type") or ""), message_type=str(event.get("message_type") or ""))
                            continue
                        inbound.extra.setdefault("account_id", self._account_id)
                        await self._fetch_media(inbound)
                        await self._queue.put(inbound)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 断线重连
                fails += 1
                if fails <= 2 or fails % 10 == 0:
                    log.warning("qq_ws_error", account_id=self._account_id,
                                error=str(exc)[:160], attempt=fails,
                                backoff_s=round(backoff, 1))
                elif fails == 3:
                    log.warning("qq_ws_error_quiet", account_id=self._account_id,
                                note="看起来 NapCat 没启动；之后每 10 次失败记一条，退避上限 300s")
            if self._closed:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)

    # ------------------------------------------------------------------
    # 收 / 发
    # ------------------------------------------------------------------

    @property
    def _beat(self) -> None:
        """告诉世界：这条通道还在听。

        契约与 `core/infra/liveness.py` 一致（pid / at_ms / beat_ms），文件名只从
        `liveness.path("qq")` 拿 —— 心跳文件名不许在本文件出现（check.py 的 hbpath 盯着）。
        """
        try:
            payload = {
                "pid": os.getpid(),
                "at_ms": int(time.time() * 1000),
                "beat_ms": BEAT_MS,
                "ws": self._ws_url,
                "connected": bool(self._connected),
            }
            jsonio.atomic_write_json(liveness.path("qq"), payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("qq_beat_failed", error=repr(exc)[:120])

    async def _beat_loop(self) -> None:
        """连接期间一直呼吸；socket 断了 `_ws_loop` 会把 `_connected` 置 False，这里就停。"""
        while not self._closed and self._connected:
            self._beat()
            await asyncio.sleep(BEAT_EVERY_S)

    def pending(self) -> int:
        """队列里未消费的消息数（供调度器即时触发同步）。"""
        return self._queue.qsize()

    async def receive(self) -> AsyncIterator[InboundMessage]:
        """把当前队列里的事件全部吐出后返回（不让同步轮阻塞）。"""
        for _ in range(_DRAIN_LIMIT):
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass  # noqa: silent(队列空：本轮已吐完，正常收尾)
                return
            yield item

    async def send(self, chat_id: str, content: str, filepath: str = "") -> SendResult:
        from core import features as _features

        if _features.dry_run_send(str(chat_id or "")):
            log.info("dry_run_send", platform="qq", chat_id=chat_id,
                     kind="file" if filepath else "text", chars=len(str(content or "")))
            return SendResult(ok=True, msg_id="dry-run")
        if not self._send_enabled:
            return SendResult(
                ok=False,
                error="QQ 发送未启用（.env 设 QQ_SEND_ENABLED=true 或配置 send_enabled）",
            )
        text = str(content or "")
        message: Any = text
        if filepath:
            path = Path(str(filepath))
            if not path.exists():
                return SendResult(ok=False, error=f"图片不存在: {filepath}")
            segments: list[dict] = []
            if text.strip():
                segments.append({"type": "text", "data": {"text": text}})
            segments.append({"type": "image", "data": {"file": path.resolve().as_uri()}})
            message = segments
        elif not text.strip():
            return SendResult(ok=False, error="空消息不发送")

        kind, target = parse_chat_id(chat_id)
        if not target:
            return SendResult(ok=False, error=f"无法解析 chat_id: {chat_id!r}")
        target_value = int(target) if target.isdigit() else target
        if kind == "group":
            action = "send_group_msg"
            payload = {"group_id": target_value, "message": message}
        else:
            action = "send_private_msg"
            payload = {"user_id": target_value, "message": message}

        try:
            async with self._send_lock:
                wait = self._last_send_at + self._send_min_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_send_at = time.monotonic()
                data = await self._api_call(action, payload)
        except Exception as exc:  # noqa: BLE001 - 网络/协议错误都算发送失败
            warn("core/execution/qq_provider.py:503 Exception", err=repr(exc))
            return SendResult(ok=False, error=f"OneBot API 调用失败: {exc}"[:300])
        if not isinstance(data, dict):
            return SendResult(ok=False, error="OneBot API 返回格式异常")
        if str(data.get("status")) == "ok" or _as_int(data.get("retcode")) == 0:
            inner = data.get("data") if isinstance(data.get("data"), dict) else {}
            return SendResult(ok=True, msg_id=str(inner.get("message_id") or ""))
        return SendResult(ok=False, error=f"OneBot 发送失败: {str(data)[:200]}")

    # ------------------------------------------------------------------
    # 媒体（批次 3）
    # ------------------------------------------------------------------

    async def _fetch_media(self, inbound: InboundMessage) -> None:
        """把 OneBot 给的媒体落到账号媒体目录（批次 3）。

        支持 http(s) 下载与 ``file://`` / 绝对路径拷贝；失败只记日志（不阻塞消息，
        下游会退回只有 url 的情况）。
        """
        if not self._media_dir or inbound.media.path:
            return
        url = str(inbound.media.url or "").strip()
        file_id = str(inbound.media.file_id or "").strip()
        source = url
        if not source and (file_id.startswith("file://") or os.path.isabs(file_id)):
            source = file_id
        if not source:
            return
        try:
            saved = await self._download_media(source, inbound.msg_id or "media")
        except Exception as exc:  # noqa: BLE001 - 媒体是尽力而为
            log.warning("qq_media_failed", account_id=self._account_id,
                        source=source[:80], error=str(exc)[:140])
            return
        if saved is not None:
            inbound.media.path = str(saved)
            log.info("qq_media_saved", account_id=self._account_id, path=str(saved))

    async def _download_media(self, source: str, name_hint: str) -> Path | None:
        target = Path(self._media_dir) / f"{_safe_name(name_hint)}{_guess_suffix(source)}"
        target.parent.mkdir(parents=True, exist_ok=True)
        text = str(source or "")
        if not (text.startswith("http://") or text.startswith("https://")):
            local = _local_path(text)
            if local is None or not local.exists():
                return None
            target.write_bytes(local.read_bytes())
            return target

        # 媒体地址常来自本机 OneBot 服务（NapCat 默认 http://127.0.0.1），
        # 明文 http 回环只在 local 车道放行，与第 729 行的 _client 同一口径。
        async with egress.async_client(lane="local", timeout_ms=15_000) as client:
            resp = await client.get(text)
            resp.raise_for_status()
            data = resp.content
        if len(data) > _MEDIA_MAX_BYTES:
            raise RuntimeError(f"图片过大（{len(data)} 字节 > {_MEDIA_MAX_BYTES}）")
        if target.suffix == ".bin":
            target = target.with_suffix(_suffix_from_content_type(resp.headers.get("content-type")))
        target.write_bytes(data)
        return target

    # ------------------------------------------------------------------
    # 通讯录（批次 2）
    # ------------------------------------------------------------------

    def _resolve_name(self, user_id: str) -> str:
        return self._member_names.get(str(user_id or ""), "")

    async def sync_directory(self, repo: Any = None, *,
                             force: bool = False,
                             min_interval_seconds: float = 1800.0) -> int:
        """同步 QQ 群列表 + 群成员 -> contacts（顺带建名字缓存）。

        默认 30 分钟最多一次（``force=True`` 可用于手动/测试）；单群失败不影响其它群。
        返回写入/更新的联系人条数。
        """
        if repo is None or not self._http_url:
            return 0
        now = time.monotonic()
        if not force and now - self._last_directory_sync < min_interval_seconds:
            return 0
        self._last_directory_sync = now
        try:
            payload = await self._api_call("get_group_list", {})
        except Exception as exc:  # noqa: BLE001 - 目录同步是尽力而为
            log.warning("qq_group_list_failed", account_id=self._account_id,
                        error=str(exc)[:160])
            return 0
        groups = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(groups, list):
            return 0
        saved = 0
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_id = str(group.get("group_id") or "")
            if not group_id:
                continue
            saved += self._upsert_contact(
                repo, f"{GROUP_PREFIX}{group_id}",
                nickname=str(group.get("group_name") or ""),
                is_group=1,
                metadata={"platform": "qq", "kind": "group"},
            )
            try:
                members_payload = await self._api_call(
                    "get_group_member_list",
                    {"group_id": int(group_id) if group_id.isdigit() else group_id},
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("qq_group_members_failed", group_id=group_id,
                            error=str(exc)[:120])
                continue
            members = members_payload.get("data") if isinstance(members_payload, dict) else None
            for member in members if isinstance(members, list) else []:
                if not isinstance(member, dict):
                    continue
                user_id = str(member.get("user_id") or "")
                if not user_id:
                    continue
                name = str(member.get("card") or member.get("nickname") or user_id)
                self._member_names[user_id] = name
                saved += self._upsert_contact(
                    repo, user_id, nickname=name, is_group=0,
                    metadata={"platform": "qq", "kind": "group_member",
                              "group_id": group_id,
                              "role": str(member.get("role") or "")},
                )
        try:
            await self.refresh_identities(repo)
        except Exception as exc:  # noqa: BLE001
            log.warning("qq_identity_refresh_failed", err=repr(exc)[:160])
        log.info("qq_directory_synced", account_id=self._account_id,
                 groups=len(groups), contacts=saved)
        return saved

    async def refresh_identities(self, repo: Any = None) -> dict:
        """2026-10-27：拉「当前的」昵称，而不是靠群成员同步碰运气。

        `get_login_info` 拿她自己（本号）现在的昵称，`get_friend_list` 拿好友现在的
        昵称/备注，都 upsert 进 contacts —— 这样用户改了 QQ 名，登录一次面板就跟着变。
        任何一步失败只记日志、不抛：同步链不该因为拿不到名字而中断。
        """
        out = {"self": 0, "friends": 0}
        try:
            login = await self._api_call("get_login_info", {})
        except Exception as exc:  # noqa: BLE001
            log.warning("qq_login_info_failed", err=repr(exc)[:160])
            login = {}
        data = login.get("data") if isinstance(login, dict) else None
        data = data if isinstance(data, dict) else {}
        uid = str(data.get("user_id") or "")
        nick = str(data.get("nickname") or "").strip()
        if uid and nick:
            self._member_names[uid] = nick
            if repo is not None:
                out["self"] = self._upsert_contact(
                    repo, uid, nickname=nick, is_group=0,
                    metadata={"platform": "qq", "kind": "self"},
                )
        try:
            payload = await self._api_call("get_friend_list", {})
        except Exception as exc:  # noqa: BLE001
            log.warning("qq_friend_list_failed", err=repr(exc)[:160])
            payload = {}
        friends = payload.get("data") if isinstance(payload, dict) else None
        for fr in friends if isinstance(friends, list) else []:
            if not isinstance(fr, dict):
                continue
            fid = str(fr.get("user_id") or "")
            fname = str(fr.get("remark") or fr.get("nickname") or "").strip()
            if not fid or not fname:
                continue
            self._member_names[fid] = fname
            if repo is not None:
                out["friends"] += self._upsert_contact(
                    repo, fid, nickname=fname, is_group=0,
                    metadata={"platform": "qq", "kind": "friend"},
                )
        log.info("qq_identity_refreshed", self_nick=nick or "", self_id=uid,
                 friends=out["friends"])
        return out

    def _upsert_contact(self, repo: Any, key: str, *, nickname: str = "",
                        is_group: int = 0, metadata: dict | None = None) -> int:
        from db.models import Contact

        try:
            contact = Contact(
                wxid=key,
                nickname=nickname,
                remark="",
                is_group=is_group,
                metadata_=json.dumps(metadata or {}, ensure_ascii=False),
                account_id=self._account_id,
            )
            repo.contacts.upsert(contact)
            return 1
        except Exception as exc:  # noqa: BLE001
            log.warning("qq_contact_upsert_failed", key=key, error=str(exc)[:120])
            return 0

    async def _api_call(self, action: str, payload: dict) -> dict:
        """调用 OneBot HTTP API；测试里可打桩替换。"""
        if not self._http_url:
            raise RuntimeError("QQ_HTTP_URL 未配置")
        if self._client is None:
            self._client = egress.async_client(lane="local", timeout_ms=10_000)
        headers = {}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        resp = await self._client.post(
            f"{self._http_url}/{action}", json=payload, headers=headers
        )
        resp.raise_for_status()
        return egress.parse_json(resp, default={})


def _ws_connect(url: str, **kwargs: Any):
    """兼容 websockets 新旧版：优先 asyncio 客户端。"""
    try:
        from websockets.asyncio.client import connect
    except Exception:  # noqa: BLE001 - 老版本
        warn("core/execution/qq_provider.py:677 Exception", err=repr(__import__("sys").exc_info()[1]))
        from websockets import connect
    return connect(url, **kwargs)


def _extra_value(account_config: Any, *names: str) -> str:
    extra = getattr(account_config, "extra", None)
    if not isinstance(extra, dict):
        return ""
    for name in names:
        value = extra.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def _float_setting(account_config: Any, *names: str) -> float:
    raw = _extra_value(account_config, *names) or os.environ.get("QQ_SEND_MIN_INTERVAL", "")
    try:
        return float(str(raw).strip()) if str(raw).strip() else 1.5
    except ValueError:
        warn("core/execution/qq_provider.py:697 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return 1.5


def build_qq_provider(account_config: Any) -> Provider:
    """工厂：从 AccountConfig（extra / 环境变量）构造 QQProvider。"""
    send_env = str(os.environ.get("QQ_SEND_ENABLED", "")).strip().lower()
    return QQProvider(
        account_id=str(getattr(account_config, "account_id", "") or ""),
        ws_url=_extra_value(account_config, "ws_url", "onebot_ws") or os.environ.get("QQ_WS_URL", ""),
        http_url=_extra_value(account_config, "http_url", "onebot_http") or os.environ.get("QQ_HTTP_URL", ""),
        access_token=_extra_value(account_config, "access_token", "token") or os.environ.get("QQ_ACCESS_TOKEN", ""),
        send_enabled=bool(getattr(account_config, "send_enabled", False))
        or send_env in {"1", "true", "yes", "on"},
        media_dir=str(getattr(account_config, "media_dir", "") or ""),
        send_min_interval=_float_setting(account_config, "send_min_interval"),
    )


register_provider_factory("qq", build_qq_provider)
