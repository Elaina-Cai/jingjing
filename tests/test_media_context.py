"""Cross-message media state / reference-resolution checks.

Run directly: python tests/test_media_context.py
"""

from __future__ import annotations

import io
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# 数据库隔离（照 tests/test_event_chain_links.py / test_command_service_db.py）：
# 本文件会经 core.bootstrap.init_singletons() 触发 init_meta_db + run_migrations
# （bootstrap 兜底、runtime 访问器等），不隔离就把 pending 迁移落到**真实**
# meta / account 库。2026-09-29 实测：Migration 51 被本套测试落到了生产库
# （data/yinyue_meta.db + 4 个账号库，applied_at 14:58:19Z）。必须在 import core.*
# 之前设好：PathSettings 在 core.config.settings **首次**导入时就取值。
# ---------------------------------------------------------------------------
_ISO_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "_test_media_context_iso"
)
os.makedirs(_ISO_ROOT, exist_ok=True)
os.environ["META_DB_PATH"] = os.path.join(_ISO_ROOT, "meta.db")
os.environ["PLATFORMS_DIR"] = os.path.join(_ISO_ROOT, "platforms")
os.environ["DATA_DIR"] = os.path.join(_ISO_ROOT, "data")

from core.config.settings import config as _config  # noqa: E402

# 守卫：env 只在 core.config.settings 首次导入前设好才有效，否则真实路径已经
# 固化进 config。这里硬失败，绝不静默把迁移/写操作落到生产库。
assert os.path.realpath(_config.paths.meta_db_path) != os.path.realpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "yinyue_meta.db"
    )
), f"测试数据库隔离失效：meta_db_path={_config.paths.meta_db_path}"


from core.conversation.media import (
    build_pending_media_message,
    extract_sticker_md5,
    is_sticker_message,
    resolve_recent_media_details,
    NAME_MEDIA_TTL_SECONDS,
    PAT_MEDIA_TTL_SECONDS,
    PendingMediaStore,
    RecentMediaStore,
    attach_batch_media,
    detect_media_reference,
    get_pending_media_store,
    get_recent_media_store,
    is_media_message,
    media_path_to_data_uri,
    pat_identity,
    record_batch_media,
    reset_media_state,
    resolve_recent_media,
)


def _msg(
    *,
    index: int = 0,
    chat_id: str = "group@chatroom",
    sender_id: str = "wxid_a",
    sender_name: str = "A",
    msg_type: str = "text",
    content: str = "",
    media_path: str = "",
    sticker_id: str = "",
    created_at: datetime | None = None,
) -> dict:
    return {
        "source": "wechat",
        "chat_id": chat_id,
        "chat_type": "group" if chat_id.endswith("@chatroom") else "private",
        "sender_id": sender_id,
        "sender_name": sender_name,
        "msg_id": f"m{index}",
        "msg_type_name": msg_type,
        "content": content,
        "media_path": media_path,
        "sticker_id": sticker_id,
        "created_at": (created_at or datetime.now()).isoformat(timespec="seconds"),
    }


def test_reference_detection() -> None:
    assert detect_media_reference("鲸鲸，看看这张图").confidence == "high"
    assert detect_media_reference("图呢？").confidence == "high"
    assert detect_media_reference("你看看").confidence == "medium"
    assert detect_media_reference("鲸鲸，今天天气不错").matched is False
    assert detect_media_reference("").matched is False
    assert is_media_message("image", "[图片]", "x.jpg") is True
    assert is_media_message("text", "[图片]", "x.jpg") is True
    assert is_media_message("text", "普通消息", "x.jpg") is False


def test_pat_identity_keeps_sender_id() -> None:
    sender_id, sender_name = pat_identity(
        '"Elaina" 拍了拍 "鲸鲸"', "wxid_seed01", "",
    )
    assert sender_id == "wxid_seed01", sender_id
    assert sender_name == "Elaina", sender_name
    sender_id, sender_name = pat_identity("普通消息", "wxid_a", "A")
    assert sender_id == "wxid_a" and sender_name == "A"


def test_recent_media_store() -> None:
    store = RecentMediaStore(ttl_seconds=100)
    now = datetime.now()
    first = store.record(
        platform="wechat", account_id="acc", chat_id="c1",
        sender_id="u1", msg_id="m1", media_path="a.jpg", now=now,
    )
    store.record(
        platform="wechat", account_id="acc", chat_id="c1",
        sender_id="u2", msg_id="m2", media_path="b.jpg", now=now,
    )
    assert first is not None
    assert store.latest(
        platform="wechat", account_id="acc", chat_id="c1",
        sender_id="u2", now=now,
    ).media_path == "b.jpg"
    assert store.latest(
        platform="wechat", account_id="acc", chat_id="c1", now=now,
    ).media_path == "b.jpg"
    # dedupe by msg_id
    again = store.record(
        platform="wechat", account_id="acc", chat_id="c1",
        sender_id="u1", msg_id="m1", media_path="a.jpg", now=now,
    )
    assert again is not None and again.media_path == "a.jpg"
    # TTL prune
    later = now + timedelta(seconds=101)
    assert store.list_items(
        platform="wechat", account_id="acc", chat_id="c1", now=later,
    ) == []


def test_pending_media_store() -> None:
    store = PendingMediaStore(ttl_seconds=100)
    now = datetime.now()
    request, created = store.add_or_replace(
        platform="wechat", account_id="acc", chat_id="c1",
        sender_id="u1", sender_name="A", question="看看这张图", now=now,
    )
    assert created is True
    request2, created2 = store.add_or_replace(
        platform="wechat", account_id="acc", chat_id="c1",
        sender_id="u1", sender_name="A", question="刚才那张图呢", now=now,
    )
    assert created2 is False
    assert request2.question == "刚才那张图呢"
    assert store.get(
        platform="wechat", account_id="acc", chat_id="c1",
        sender_id="u1", now=now,
    ) is not None
    later = now + timedelta(seconds=101)
    assert store.get(
        platform="wechat", account_id="acc", chat_id="c1",
        sender_id="u1", now=later,
    ) is None
    # sender_id may be "unknown" on some group pats; display name still matches.
    store.add_or_replace(
        platform="wechat", account_id="acc", chat_id="c1",
        sender_id="u1", sender_name="A", question="看看这张图", now=now,
    )
    matched = store.pop_match(
        platform="wechat", account_id="acc", chat_id="c1",
        sender_id="unknown", sender_name="A", now=now,
    )
    assert matched is not None and matched.sender_id == "u1", matched
    store.clear()  # 这是 pending-media store，不是 TerminalEventStore（U-00 只管账本）


def test_attach_batch_both_orders() -> None:
    # text first, image next
    batch = [
        _msg(index=0, content="鲸鲸，看看这张图", sender_id="u1"),
        _msg(index=1, msg_type="image", content="[图片]", media_path="a.jpg",
             sender_id="u1"),
    ]
    out = attach_batch_media(batch, persona_names=["鲸鲸"])
    assert out.get(0) == ["a.jpg"], out
    assert batch[0]["_attached_media_paths"] == ["a.jpg"]

    # image first, plain name after
    batch = [
        _msg(index=0, msg_type="image", content="[图片]", media_path="b.jpg",
             sender_id="u1"),
        _msg(index=1, content="鲸鲸，XXXXXXX", sender_id="u1"),
    ]
    out = attach_batch_media(batch, persona_names=["鲸鲸"])
    assert out.get(1) == ["b.jpg"], out

    # explicit reference can reach a few messages back
    batch = [
        _msg(index=0, msg_type="image", content="[图片]", media_path="c.jpg",
             sender_id="u1"),
        _msg(index=1, content="无关消息", sender_id="u2"),
        _msg(index=2, content="鲸鲸，刚才那张图再讲一下", sender_id="u1"),
    ]
    out = attach_batch_media(batch, persona_names=["鲸鲸"])
    assert out.get(2) == ["c.jpg"], out

    # unrelated text must not attach
    batch = [
        _msg(index=0, msg_type="image", content="[图片]", media_path="d.jpg",
             sender_id="u1"),
        _msg(index=1, content="今天天气不错", sender_id="u1"),
    ]
    out = attach_batch_media(batch, persona_names=["鲸鲸"])
    assert out == {}, out

    # another sender's image must not attach to a bare name
    batch = [
        _msg(index=0, msg_type="image", content="[图片]", media_path="e.jpg",
             sender_id="u2"),
        _msg(index=1, content="鲸鲸，你看下", sender_id="u1"),
    ]
    out = attach_batch_media(batch, persona_names=["鲸鲸"])
    assert out == {}, out


def test_resolve_recent_media() -> None:
    reset_media_state()
    store = get_recent_media_store()
    now = datetime.now()
    store.record(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
        sender_id="u1", msg_id="m1", media_path="recent.jpg", now=now,
    )
    assert resolve_recent_media(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
        sender_id="u1", is_group=True, trigger_reason="mentioned_in_group",
        content="鲸鲸，看看这张图", now=now,
    ) == ["recent.jpg"]
    assert resolve_recent_media(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
        sender_id="u1", is_group=True, trigger_reason="patted_in_group",
        content='"A" 拍了拍 "鲸鲸"', now=now,
    ) == ["recent.jpg"]
    # stale image beyond pat ttl
    later = now + timedelta(seconds=PAT_MEDIA_TTL_SECONDS + 1)
    assert resolve_recent_media(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
        sender_id="u1", is_group=True, trigger_reason="patted_in_group",
        content='"A" 拍了拍 "鲸鲸"', now=later,
    ) == []
    # long unrelated text after image must not attach
    assert resolve_recent_media(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
        sender_id="u1", is_group=True, trigger_reason="mentioned_in_group",
        content="鲸鲸，我们刚才讨论的部署方案你记一下，下午开会要用",
        now=now,
    ) == []
    reset_media_state()


def test_record_batch_media_and_data_uri() -> None:
    reset_media_state()
    batch = [
        _msg(index=0, msg_type="image", content="[图片]", media_path="x.jpg"),
    ]
    assert record_batch_media(batch, account_id="acc") == 1
    assert get_recent_media_store().latest(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
    ) is not None

    try:
        from PIL import Image
    except Exception:  # pragma: no cover - PIL is part of the runtime
        Image = None
    if Image is not None:
        tmp = Path("data/_test_media_context_tmp.png")
        try:
            Image.new("RGB", (8, 8), (255, 0, 0)).save(tmp)
            uri = media_path_to_data_uri(str(tmp))
            assert uri.startswith("data:image/"), uri[:40]
            assert "base64," in uri
        finally:
            tmp.unlink(missing_ok=True)
    assert media_path_to_data_uri("does-not-exist.jpg") == ""
    reset_media_state()


def test_build_pending_media_message() -> None:
    request = PendingMediaStore(ttl_seconds=100)
    pending, _ = request.add_or_replace(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
        sender_id="u1", sender_name="A", question="鲸鲸，看看这张图",
        now=datetime.now(),
    )
    image_msg = _msg(index=5, msg_type="image", content="[图片]",
                     media_path="pending.jpg", sender_id="u1")
    synthetic = build_pending_media_message(pending, image_msg, platform="wechat")
    assert synthetic["content"] == "鲸鲸，看看这张图"
    assert synthetic["msg_type_name"] == "text"
    assert synthetic["_attached_media_paths"] == ["pending.jpg"]
    assert "mention_self" in synthetic["extra"]


def test_conversational_recent_media_resolution() -> None:
    from types import SimpleNamespace
    from agents.conversational import ConversationalAgent
    from PIL import Image

    reset_media_state()
    tmp = Path("data/_test_media_resolve_tmp.png")
    try:
        Image.new("RGB", (8, 8), (0, 128, 255)).save(tmp)
        now = datetime.now()
        get_recent_media_store().record(
            platform="wechat", account_id="acc", chat_id="group@chatroom",
            sender_id="u1", msg_id="m1", media_path=str(tmp),
            now=now,
        )
        agent = ConversationalAgent.__new__(ConversationalAgent)
        agent._turn_decision = SimpleNamespace(
            reason="patted_in_group", trigger_reasons=("patted_in_group",),
            source="direct",
        )
        agent._reason_text = "patted_in_group"
        context = SimpleNamespace(extra={
            "platform": "wechat", "account_id": "acc",
            "chat_id": "group@chatroom", "sender_id": "u1",
            "is_group": True,
        })
        resolved = agent._resolve_recent_images(
            context, '"A" 拍了拍 "鲸鲸"',
        )
        assert len(resolved) == 1, resolved
        assert resolved[0].startswith("data:image/")
        # A non-media trigger with no reference must not consume the image.
        agent._turn_decision = SimpleNamespace(
            reason="unrelated", trigger_reasons=("unrelated",), source="direct",
        )
        agent._reason_text = "unrelated"
        assert agent._resolve_recent_images(context, "今天天气不错") == []
    finally:
        tmp.unlink(missing_ok=True)
        reset_media_state()

def test_quoted_sticker_wins_over_recent() -> None:
    """引用表情包时：识别被引用的那张（md5），而不是最近另一张。"""
    reset_media_state()
    now = datetime.now()
    get_recent_media_store().record(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
        sender_id="wxid_pan", sender_name="P5", msg_id="m1",
        kind="sticker", sticker_id="b" * 32, description="粉发少女端碗大哭",
        now=now,
    )
    details = resolve_recent_media_details(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
        sender_id="wxid_pan", sender_name="P5", is_group=True,
        trigger_reason="mentioned_in_group",
        content="[引用 P5: wxid_pan:123:0:" + "a" * 32 + "::0] @鲸鲸 识图",
    )
    assert details.sticker_ids == ["a" * 32], details
    assert "粉发少女" not in "".join(details.descriptions), details
    assert details.items and details.items[0].sender_name == "P5", details
    reset_media_state()


def test_quoted_text_does_not_borrow_recent_media() -> None:
    """引用的是文本时：不要拿「最近一张」充数（23:07 群聊事故的修复）。"""
    reset_media_state()
    now = datetime.now()
    get_recent_media_store().record(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
        sender_id="wxid_pan", sender_name="P5", msg_id="m1",
        kind="sticker", sticker_id="b" * 32, description="粉发少女端碗大哭",
        now=now,
    )
    details = resolve_recent_media_details(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
        sender_id="wxid_pan", sender_name="P5", is_group=True,
        trigger_reason="mentioned_in_group",
        content="[引用 鲸鲸: @P5 这不就是那只QQ企鹅疯狂摇头嘛] 你有这个表情吗？看看",
    )
    assert details.empty, details
    reset_media_state()

def test_description_cache() -> None:
    from core.conversation.media import (
        cached_image_description,
        get_media_description_cache,
        is_generic_image_request,
        remember_image_description,
    )

    assert is_generic_image_request("看看")
    assert is_generic_image_request("这张图是什么")
    assert is_generic_image_request("鲸鲸，这张图里的报价是多少") is False
    reset_media_state()
    cache = get_media_description_cache()
    tmp = Path("data/_test_media_cache_tmp.png")
    try:
        from PIL import Image
        Image.new("RGB", (8, 8), (0, 0, 0)).save(tmp)
        remember_image_description([str(tmp)], "一只黑色的猫")
        assert cached_image_description([str(tmp)]) == "一只黑色的猫"
        assert cache.get(str(tmp)) == "一只黑色的猫"
    finally:
        tmp.unlink(missing_ok=True)
        reset_media_state()


def test_sender_fallback_by_display_name() -> None:
    reset_media_state()
    store = get_recent_media_store()
    now = datetime.now()
    store.record(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
        sender_id="wxid_real", sender_name="Elaina",
        msg_id="m-real", media_path="real.jpg", now=now,
    )
    # group pats can have sender_id == "unknown"; display name still matches.
    assert resolve_recent_media(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
        sender_id="unknown", sender_name="Elaina", is_group=True,
        trigger_reason="patted_in_group",
        content='"Elaina" 拍了拍 "鲸鲸"', now=now,
    ) == ["real.jpg"]
    batch = [
        _msg(index=0, msg_type="image", content="[图片]", media_path="real.jpg",
             sender_id="wxid_real", sender_name="Elaina"),
        _msg(index=1, msg_type="pat", content='"Elaina" 拍了拍 "鲸鲸"',
             sender_id="unknown", sender_name="Elaina"),
    ]
    out = attach_batch_media(batch, persona_names=["鲸鲸"])
    assert out.get(1) == ["real.jpg"], out
    reset_media_state()


def test_pat_pending_kind_and_drop() -> None:
    store = PendingMediaStore(ttl_seconds=100)
    now = datetime.now()
    request, created = store.add_or_replace(
        platform="wechat", account_id="acc", chat_id="c1",
        sender_id="u1", sender_name="A", question="",
        trigger="patted_in_group", kind="pat", now=now,
    )
    assert created and request.kind == "pat"
    synthetic = build_pending_media_message(
        request,
        _msg(index=0, msg_type="image", content="[图片]",
             media_path="pat.jpg", sender_id="u1"),
    )
    assert synthetic["content"] == "[图片]", synthetic
    assert synthetic["_attached_media_paths"] == ["pat.jpg"]
    dropped = store.drop(
        platform="wechat", account_id="acc", chat_id="c1",
        sender_id="u1", kind="pat", now=now,
    )
    assert dropped is not None and dropped.kind == "pat"
    assert store.get(
        platform="wechat", account_id="acc", chat_id="c1",
        sender_id="u1", now=now,
    ) is None


def test_media_path_from_message_row() -> None:
    from types import SimpleNamespace
    from core.conversation.media import media_path_from_message_row

    image_row = SimpleNamespace(
        media_path="x.jpg", msg_type_name="image", content="[图片]",
    )
    assert media_path_from_message_row(image_row) == "x.jpg"
    text_row = SimpleNamespace(
        media_path="x.jpg", msg_type_name="text", content="普通消息",
    )
    assert media_path_from_message_row(text_row) == ""
    empty_row = SimpleNamespace(
        media_path="", msg_type_name="image", content="[图片]",
    )
    assert media_path_from_message_row(empty_row) == ""


def test_vision_augmented_message() -> None:
    from agents.conversational import build_vision_augmented_message

    text = build_vision_augmented_message(
        "一只橘猫趴在窗台上",
        "[引用 某人: [媒体消息]]\n@鲸鲸 识别图片",
    )
    assert "[图片分析] 一只橘猫趴在窗台上" in text
    assert "[引用" not in text
    assert "禁止说看不到图片" in text
    assert "不要复述图片内容" in text
    assert "@鲸鲸" in text


def test_sticker_reference_and_md5() -> None:
    md5 = "a" * 32
    assert detect_media_reference("鲸鲸，看看这个表情").confidence == "high"
    assert detect_media_reference("这个表情包呢").matched is True
    assert extract_sticker_md5(f"[表情包:{md5}]") == md5
    assert extract_sticker_md5({"sticker_id": md5}) == md5
    assert extract_sticker_md5({"content": f"[表情包:{md5}]"}) == md5
    assert is_sticker_message("sticker", f"[表情包:{md5}]", md5) is True
    assert is_sticker_message("text", "普通消息", "") is False


def test_sticker_store_and_resolution() -> None:
    reset_media_state()
    md5 = "b" * 32
    get_recent_media_store().record(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
        sender_id="u1", msg_id="s1", media_path="",
        sticker_id=md5, description="捂脸大笑",
    )
    details = resolve_recent_media_details(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
        sender_id="u1", is_group=True, trigger_reason="mentioned_in_group",
        content="鲸鲸，看看这个表情",
    )
    assert details.paths == []
    assert details.sticker_ids == [md5]
    assert details.descriptions == ["捂脸大笑"]
    assert resolve_recent_media(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
        sender_id="u1", is_group=True, trigger_reason="mentioned_in_group",
        content="鲸鲸，看看这个表情",
    ) == []
    reset_media_state()


def test_record_batch_sticker_without_path() -> None:
    reset_media_state()
    md5 = "c" * 32
    batch = [
        _msg(index=0, msg_type="sticker", content=f"[表情包:{md5}]",
             sticker_id=md5),
    ]
    assert record_batch_media(
        batch, account_id="acc",
        description_lookup=lambda value: "捂脸大笑" if value == md5 else "",
    ) == 1
    item = get_recent_media_store().latest(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
    )
    assert item is not None
    assert item.sticker_id == md5
    assert item.description == "捂脸大笑"
    reset_media_state()


def test_build_pending_sticker_message() -> None:
    md5 = "d" * 32
    pending, _ = PendingMediaStore(ttl_seconds=100).add_or_replace(
        platform="wechat", account_id="acc", chat_id="group@chatroom",
        sender_id="u1", sender_name="A", question="鲸鲸，看看这个表情",
    )
    synthetic = build_pending_media_message(
        pending,
        _msg(index=0, msg_type="sticker", content=f"[表情包:{md5}]",
             sticker_id=md5),
        sticker_id=md5, description="捂脸大笑",
    )
    assert synthetic["_attached_sticker_id"] == md5
    assert synthetic["_sticker_description"] == "捂脸大笑"


def test_emoji_dat_image_sniffing() -> None:
    from core.preprocess import emoji_recognition as er

    md5 = "f" * 32
    path = Path("data/emoji_cache") / f"{md5}.dat"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal 1x1 GIF89a
    gif = (
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00"
        b"\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,"
        b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
    )
    try:
        path.write_bytes(gif)
        assert er._sniff_image(gif) == ("gif", "image/gif")
        found = er._find_emoji_image(md5)
        assert found is not None and found.name == f"{md5}.dat", found
    finally:
        path.unlink(missing_ok=True)


def main() -> bool:
    tests = [
        test_reference_detection,
        test_pat_identity_keeps_sender_id,
        test_recent_media_store,
        test_pending_media_store,
        test_attach_batch_both_orders,
        test_resolve_recent_media,
        test_record_batch_media_and_data_uri,
        test_build_pending_media_message,
        test_conversational_recent_media_resolution,
        test_quoted_sticker_wins_over_recent,
        test_quoted_text_does_not_borrow_recent_media,
        test_description_cache,
        test_sender_fallback_by_display_name,
        test_pat_pending_kind_and_drop,
        test_media_path_from_message_row,
        test_vision_augmented_message,
        test_sticker_reference_and_md5,
        test_sticker_store_and_resolution,
        test_record_batch_sticker_without_path,
        test_build_pending_sticker_message,
        test_emoji_dat_image_sniffing,
    ]
    failed = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print("\nall passed" if not failed else f"\nfailed: {', '.join(failed)}")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
