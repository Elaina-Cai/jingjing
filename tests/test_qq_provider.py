"""QQ Provider（OneBot v11 / NapCat）回归测试。

不需要真 QQ：
- 事件解析用纯函数断言（私聊/群 @/引用/自己发的/非消息事件）；
- 收发用本地假 OneBot（临时 WS 服务 + 打桩 HTTP API）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.execution.provider import MsgType, Platform, create_provider  # noqa: E402
from core.execution.qq_provider import (  # noqa: E402
    QQProvider,
    inbound_from_event,
    parse_chat_id,
)

EVENT_PRIVATE = {
    "post_type": "message",
    "message_type": "private",
    "self_id": 10001,
    "user_id": 20002,
    "message_id": 90001,
    "time": 1758780000,
    "raw_message": "你好呀",
    "sender": {"nickname": "小明", "card": ""},
    "message": [{"type": "text", "data": {"text": "你好呀"}}],
}

EVENT_GROUP_AT = {
    "post_type": "message",
    "message_type": "group",
    "self_id": 10001,
    "user_id": 20003,
    "group_id": 333333,
    "message_id": 90002,
    "time": 1758780010,
    "raw_message": "[CQ:at,qq=10001] 在吗",
    "sender": {"nickname": "小红", "card": "红红"},
    "message": [
        {"type": "at", "data": {"qq": "10001", "name": "鲸鲸"}},
        {"type": "text", "data": {"text": " 在吗"}},
    ],
}

EVENT_GROUP_REPLY = {
    "post_type": "message",
    "message_type": "group",
    "self_id": 10001,
    "user_id": 20004,
    "group_id": 333333,
    "message_id": 90003,
    "time": 1758780020,
    "raw_message": "收到",
    "sender": {"nickname": "小刚", "card": ""},
    "message": [
        {"type": "reply", "data": {"id": 88888, "text": "上一句原话"}},
        {"type": "text", "data": {"text": "收到"}},
    ],
}


def _provider(**overrides) -> QQProvider:
    options = dict(
        account_id="10001",
        ws_url="",
        http_url="http://127.0.0.1:9",
        send_enabled=True,
    )
    options.update(overrides)
    return QQProvider(**options)


def test_parse_chat_id() -> None:
    assert parse_chat_id("group:123") == ("group", "123")
    assert parse_chat_id("user:456") == ("private", "456")
    assert parse_chat_id("789") == ("private", "789")  # 无前缀按私聊，保守
    assert parse_chat_id("") == ("private", "")


def test_inbound_private_text() -> None:
    inbound = inbound_from_event(EVENT_PRIVATE)
    assert inbound is not None
    assert inbound.platform == Platform.QQ
    assert inbound.chat_id == "user:20002"
    assert inbound.user_id == "20002"
    assert inbound.user_name == "小明"
    assert inbound.content == "你好呀"
    assert inbound.msg_type == MsgType.TEXT
    assert inbound.is_group is False
    assert inbound.extra["mention_self"] is False
    assert inbound.extra["message_type"] == "private"


def test_inbound_group_at_sets_mention_self() -> None:
    inbound = inbound_from_event(EVENT_GROUP_AT)
    assert inbound is not None
    assert inbound.chat_id == "group:333333"
    assert inbound.is_group is True
    assert inbound.content == "@鲸鲸 在吗"
    assert inbound.mentions and inbound.mentions[0].user_id == "10001"
    assert inbound.extra["mention_self"] is True
    assert inbound.extra["group_id"] == "333333"
    assert inbound.user_name == "红红"  # 群名片优先


def test_inbound_reply_segment() -> None:
    inbound = inbound_from_event(EVENT_GROUP_REPLY)
    assert inbound is not None
    assert inbound.msg_type == MsgType.QUOTE_REPLY
    assert inbound.quote.reply_to_id == "88888"
    assert inbound.quote.reply_content == "上一句原话"
    assert inbound.content == "收到"


def test_self_message_skipped() -> None:
    event = dict(EVENT_PRIVATE)
    event["user_id"] = 10001  # 自己发的
    assert inbound_from_event(event) is None


def test_non_message_event_skipped() -> None:
    assert inbound_from_event({"post_type": "notice", "notice_type": "poke"}) is None
    assert inbound_from_event(None) is None
    broken = dict(EVENT_PRIVATE)
    broken.pop("user_id")
    assert inbound_from_event(broken) is None


def test_send_group_and_private() -> None:
    async def scenario():
        provider = _provider()
        calls: list[tuple[str, dict]] = []

        async def fake_call(action: str, payload: dict):
            calls.append((action, payload))
            return {"status": "ok", "retcode": 0, "data": {"message_id": 42}}

        provider._api_call = fake_call  # type: ignore[method-assign]
        group_result = await provider.send("group:333333", "hi")
        private_result = await provider.send("user:20002", "hello")
        return calls, group_result, private_result

    calls, group_result, private_result = asyncio.run(scenario())
    assert calls[0] == ("send_group_msg", {"group_id": 333333, "message": "hi"})
    assert calls[1] == ("send_private_msg", {"user_id": 20002, "message": "hello"})
    assert group_result.ok and group_result.msg_id == "42"
    assert private_result.ok


def test_send_refuses() -> None:
    async def scenario():
        disabled = _provider(send_enabled=False)
        r1 = await disabled.send("group:1", "hi")
        with_file = _provider()
        r2 = await with_file.send("group:1", "hi", filepath="C:/x.png")
        empty = _provider()
        r3 = await empty.send("group:1", "   ")
        bad_id = _provider()
        r4 = await bad_id.send("group:", "hi")
        return r1, r2, r3, r4

    r1, r2, r3, r4 = asyncio.run(scenario())
    assert not r1.ok and "未启用" in r1.error
    assert not r2.ok and "不存在" in r2.error
    assert not r3.ok and "空消息" in r3.error
    assert not r4.ok and "chat_id" in r4.error


def test_send_api_failure() -> None:
    async def scenario():
        provider = _provider()

        async def failed(action: str, payload: dict):
            return {"status": "failed", "retcode": 100, "msg": "bad"}

        provider._api_call = failed  # type: ignore[method-assign]
        r1 = await provider.send("group:1", "hi")

        async def explode(action: str, payload: dict):
            raise RuntimeError("connection refused")

        provider._api_call = explode  # type: ignore[method-assign]
        r2 = await provider.send("group:1", "hi")
        return r1, r2

    r1, r2 = asyncio.run(scenario())
    assert not r1.ok and "发送失败" in r1.error
    assert not r2.ok and "API 调用失败" in r2.error


def test_factory_and_registration() -> None:
    account = SimpleNamespace(
        platform="qq",
        account_id="10001",
        send_enabled=True,
        extra={
            "ws_url": "ws://127.0.0.1:9",
            "http_url": "http://127.0.0.1:9",
            "access_token": "tok",
        },
    )
    provider = create_provider(account)
    assert isinstance(provider, QQProvider)
    assert provider.platform == Platform.QQ
    assert provider.capabilities == frozenset({"text", "image"})
    assert provider._account_id == "10001"
    assert provider._ws_url == "ws://127.0.0.1:9"
    assert provider._access_token == "tok"


def test_ws_receive_integration() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    async def scenario():
        try:
            from websockets.asyncio.server import serve
        except Exception:  # noqa: BLE001 - 老版本
            import websockets

            serve = websockets.serve

        async def handler(ws):
            await ws.send(json.dumps(EVENT_PRIVATE, ensure_ascii=False))
            await asyncio.sleep(0.5)

        async with serve(handler, "127.0.0.1", port):
            provider = QQProvider(
                account_id="10001",
                ws_url=f"ws://127.0.0.1:{port}",
                http_url="",
                send_enabled=False,
            )
            assert await provider.connect()
            item = None
            for _ in range(80):
                async for message in provider.receive():
                    item = message
                    break
                if item is not None:
                    break
                await asyncio.sleep(0.05)
            await provider.disconnect()
            return item

    item = asyncio.run(scenario())
    assert item is not None, "WS 事件没有被收进队列"
    assert item.platform == Platform.QQ
    assert item.chat_id == "user:20002"
    assert item.content == "你好呀"
    assert item.extra.get("mention_self") is False


def test_provider_ingestor_fallback() -> None:
    from core.ingestion.ingestors import ProviderAccountIngestor, get_ingestion_manager

    provider = _provider()
    ctx = SimpleNamespace(platform="qq", account_id="10001", provider=provider)
    assert isinstance(get_ingestion_manager().for_context(ctx), ProviderAccountIngestor)
    # 没有 Provider 的账号不会被兜底（该轮直接跳过，不会误报）
    empty = SimpleNamespace(platform="qq", account_id="x", provider=None)
    assert get_ingestion_manager().for_context(empty) is None


EVENT_POKE_GROUP = {
    "post_type": "notice",
    "notice_type": "poke",
    "self_id": 10001,
    "user_id": 20005,
    "target_id": 10001,
    "group_id": 333333,
    "time": 1758780030,
}

EVENT_POKE_SELF = {
    "post_type": "notice",
    "notice_type": "poke",
    "self_id": 10001,
    "user_id": 10001,
    "target_id": 20005,
    "group_id": 333333,
    "time": 1758780040,
}


class _FakeContacts:
    def __init__(self) -> None:
        self.rows: list = []

    def upsert(self, contact) -> None:
        self.rows.append(contact)


class _FakeRepo:
    def __init__(self) -> None:
        self.contacts = _FakeContacts()


def test_poke_notice_group() -> None:
    names = {"20005": "P7", "10001": "鲸鲸"}
    inbound = inbound_from_event(EVENT_POKE_GROUP, bot_id="10001",
                                 resolve_name=names.get)
    assert inbound is not None
    assert inbound.msg_type == MsgType.PAT
    assert inbound.chat_id == "group:333333"
    assert inbound.is_group is True
    assert inbound.content == '"P7" 拍了拍 "鲸鲸"'
    assert inbound.user_id == "20005"
    assert inbound.extra.get("mention_self") is True
    assert inbound.extra.get("target_id") == "10001"


def test_poke_notice_self() -> None:
    inbound = inbound_from_event(EVENT_POKE_SELF, bot_id="10001",
                                 resolve_name={"20005": "P7"}.get)
    assert inbound is not None
    assert inbound.content == '我拍了拍 "P7"'
    assert inbound.extra.get("is_self") is True
    assert inbound.extra.get("mention_self") is False


def test_non_poke_notice_skipped() -> None:
    event = dict(EVENT_POKE_GROUP, notice_type="group_recall")
    assert inbound_from_event(event, bot_id="10001") is None


def test_sync_directory_upserts_contacts() -> None:
    provider = _provider()
    repo = _FakeRepo()
    calls: list[str] = []

    async def fake_call(action: str, payload: dict):
        calls.append(action)
        if action == "get_group_list":
            return {"status": "ok",
                    "data": [{"group_id": 333333, "group_name": "作业群"}]}
        if action == "get_group_member_list":
            assert payload["group_id"] == 333333
            return {"status": "ok", "data": [
                {"user_id": 20003, "nickname": "小红", "card": "红红", "role": "member"},
                {"user_id": 20005, "nickname": "P7", "card": "", "role": "admin"},
            ]}
        raise AssertionError(action)

    provider._api_call = fake_call  # type: ignore[method-assign]

    async def run(force: bool = True) -> int:
        return await provider.sync_directory(repo, force=force)

    saved = asyncio.run(run())
    assert saved == 3, saved
    keys = {row.wxid for row in repo.contacts.rows}
    assert keys == {"group:333333", "20003", "20005"}, keys
    group_row = next(r for r in repo.contacts.rows if r.wxid == "group:333333")
    assert group_row.is_group == 1
    assert group_row.nickname == "作业群"
    member_row = next(r for r in repo.contacts.rows if r.wxid == "20003")
    assert member_row.nickname == "红红"
    assert provider._member_names["20005"] == "P7"
    # 节流：刚同步过，非 force 不再打 API
    again = asyncio.run(run(force=False))
    assert again == 0
    assert calls.count("get_group_list") == 1


def test_group_chat_id_helpers() -> None:
    from core.contracts.events import ChatType
    from core.identity.platforms import chat_type_for, is_group_chat
    from db.models import is_group_chat_id

    assert is_group_chat_id("group:333333") is True
    assert is_group_chat_id("123@chatroom") is True
    assert is_group_chat_id("user:20002") is False
    assert is_group_chat("qq", chat_id="group:333333") is True
    assert is_group_chat("qq", chat_id="user:20002") is False
    assert chat_type_for("qq", chat_id="group:333333") == ChatType.GROUP
    assert chat_type_for("wechat", chat_id="123@chatroom") == ChatType.GROUP


EVENT_GROUP_IMAGE = {
    "post_type": "message",
    "message_type": "group",
    "self_id": 10001,
    "user_id": 20006,
    "group_id": 333333,
    "message_id": 90004,
    "time": 1758780050,
    "raw_message": "[CQ:image,file=abc.jpg]",
    "sender": {"nickname": "小美", "card": ""},
    "message": [{"type": "image", "data": {"url": "", "file": "abc.jpg"}}],
}


def _tmp_dir(name: str) -> Path:
    """沙箱里 TemporaryDirectory 清理会 PermissionError，用仓库内目录。"""
    path = ROOT / f".tmp_qq_{name}"
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _serve_bytes(payload: bytes, content_type: str = "image/png"):
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # noqa: D102
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/img.png"


def test_image_message_fields() -> None:
    inbound = inbound_from_event(EVENT_GROUP_IMAGE, bot_id="10001")
    assert inbound is not None
    assert inbound.msg_type == MsgType.IMAGE
    assert inbound.media.file_id == "abc.jpg"
    assert inbound.content == "[图片]"


def test_media_download_and_send_image() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"
    server, url = _serve_bytes(png)
    tmp = _tmp_dir("media")
    try:
        provider = _provider(media_dir=str(tmp), send_min_interval=0)
        inbound = inbound_from_event(EVENT_GROUP_IMAGE, bot_id="10001")
        assert inbound is not None
        inbound.media.url = url

        async def run_fetch() -> None:
            await provider._fetch_media(inbound)

        asyncio.run(run_fetch())
        assert inbound.media.path, "图片没有落盘"
        assert Path(inbound.media.path).read_bytes() == png
        assert Path(inbound.media.path).suffix == ".png"

        sent: list[tuple[str, dict]] = []

        async def fake_call(action: str, payload: dict):
            sent.append((action, payload))
            return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        provider._api_call = fake_call  # type: ignore[method-assign]

        async def run_send():
            return await provider.send("group:333333", "", filepath=inbound.media.path)

        result = asyncio.run(run_send())
        assert result.ok, result.error
        action, payload = sent[-1]
        assert action == "send_group_msg"
        message = payload["message"]
        assert isinstance(message, list) and len(message) == 1
        assert message[0]["type"] == "image"
        assert str(message[0]["data"]["file"]).startswith("file://")
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_media_local_file_copy() -> None:
    tmp = _tmp_dir("local")
    try:
        src = tmp / "src.jpg"
        src.write_bytes(b"jpeg-bytes")
        provider = _provider(media_dir=str(tmp / "out"), send_min_interval=0)
        inbound = inbound_from_event(EVENT_GROUP_IMAGE, bot_id="10001")
        assert inbound is not None
        inbound.media.url = src.as_uri()

        async def run_fetch() -> None:
            await provider._fetch_media(inbound)

        asyncio.run(run_fetch())
        assert inbound.media.path
        assert Path(inbound.media.path).read_bytes() == b"jpeg-bytes"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_send_throttle_interval() -> None:
    provider = _provider(send_min_interval=0.25)
    calls: list[float] = []

    async def fake_call(action: str, payload: dict):
        calls.append(time.monotonic())
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

    provider._api_call = fake_call  # type: ignore[method-assign]

    async def run() -> None:
        await provider.send("user:20002", "一")
        await provider.send("user:20002", "二")

    asyncio.run(run())
    assert len(calls) == 2
    assert calls[1] - calls[0] >= 0.2, calls


def test_send_log_extra_for_qq() -> None:
    from core.speech.outbound import build_send_log_extra

    extra = json.loads(build_send_log_extra("qq", "group:333333"))
    assert extra == {"group_id": "333333"}
    assert build_send_log_extra("qq", "user:20002") == ""
    assert build_send_log_extra("wechat", "123@chatroom") == ""


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"qq provider checks: {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
