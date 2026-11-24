"""Media registry checks: clues, resolution, retries, descriptions.

The registry exists because a picture whose file was not on disk at ingest time
used to be lost permanently -- the md5 that would have allowed a second attempt
was discarded.  These checks pin the parts that make recovery possible:

* the md5 is readable from every ``packed_info_data`` variant real messages use
* a message is registered whether or not its file existed yet
* a reply is registered only when it quotes real media
* resolution fills a blank ``messages.media_path`` and never overwrites one
* the retry pass moves rows through resolved / missing with backoff
* a description survives on the row and reaches the rendered transcript

Everything runs against a throwaway database and real image files that already
exist on disk; nothing here writes to a live database.

Run directly: python tests/test_media_registry.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from core.conversation.media_registry import (  # noqa: E402
    KIND_IMAGE,
    KIND_QUOTE_IMAGE,
    STATUS_MISSING,
    STATUS_PENDING,
    STATUS_RESOLVED,
    lookup_description,
    nearby_resolved_path,
    quoted_is_media,
    register,
    register_from_message,
    remember_description,
    resolve_once,
    resolve_pending,
    session_dir_for,
)
from db.content_extract import md5_from_packed_info  # noqa: E402
from db.migrations import run_migrations  # noqa: E402
from db.models import Message, MessageMedia, init_db  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "data" / "_test_media_registry.db"

# Real payloads captured from a live account database.
PACKED_42B = bytes.fromhex(
    "080310021a22222064623164613233623435333136343864353832343637346163323238653134385800"
)
PACKED_46B = bytes.fromhex(
    "0802100c1a2808f00410830922206633633231343764653161393237643934363634353363353239316164613335"
)
PACKED_45B = bytes.fromhex(
    "0802100c1a27084010da0822203661373435366335313461316635663136346237386235623336356630333839"
)
MD5_42B = "db1da23b4531648d5824674ac228e148"
MD5_46B = "f3c2147de1a927d9466453c5291ada35"
MD5_45B = "6a7456c514a1f5f164b78b5b365f0389"

# A file that already exists from an earlier run of the real pipeline; the
# resolver checks the filesystem, so a real file is what makes this meaningful.
SAMPLE_IMAGE = _ROOT / "data" / "image_cache" / f"{MD5_42B}_b.jpg"


def _cleanup() -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(_DB) + suffix).unlink(missing_ok=True)
    # Any decrypted sample this run produced.
    for extra in _ROOT.glob(f"data/image_cache/{MD5_46B}*"):
        extra.unlink(missing_ok=True)


def _fresh_engine():
    _cleanup()
    engine = init_db(str(_DB))
    run_migrations(str(_DB))
    return engine


def test_session_dir_matches_wechat_layout() -> bool:
    """The session directory is md5(chat_id) -- verified against a real account."""
    print("\n[会话目录命名]")
    cases = {
        "wxid_seed01": "b1ed75de0a45290ff6f5f3b654b81d4c",
        "10000000001@chatroom": "29a462754f841a0be85bc6f94abb46c9",
    }
    ok = True
    for chat_id, expected in cases.items():
        got = session_dir_for(chat_id)
        good = got == expected
        ok &= good
        print(f"  {'✓' if good else '✗'} {chat_id} -> {got[:16]}…")
    return bool(ok)


def test_md5_from_every_payload_variant() -> bool:
    """Every real payload shape yields its md5, including the ones the field
    walker returns nothing for."""
    print("\n[md5 提取（三种真实 payload）]")
    cases = [
        ("42B 字段可遍历", PACKED_42B, MD5_42B),
        ("46B 遍历返回空", PACKED_46B, MD5_46B),
        ("45B 遍历返回空", PACKED_45B, MD5_45B),
        ("空 payload", b"", ""),
        ("垃圾数据", b"\x00\x01\x02not-a-payload", ""),
        ("全同字符 hex", b"a" * 32, ""),
    ]
    ok = True
    for label, payload, expected in cases:
        got = md5_from_packed_info(payload)
        good = got == expected
        ok &= good
        print(f"  {'✓' if good else '✗'} {label}: {got or '(空)'}")
    return bool(ok)


def test_registration_rules() -> bool:
    """What gets a row: media messages always, replies only when they quote media."""
    print("\n[注册规则]")
    engine = _fresh_engine()
    ok = True
    try:
        with Session(engine) as session:
            session.add(Message(msg_id=1001, chat_id="c1", msg_type_name="image",
                                content="[图片]", account_id="a1"))
            session.add(Message(msg_id=1002, chat_id="c1", msg_type_name="text",
                                content="你好", account_id="a1"))
            session.add(Message(msg_id=1003, chat_id="c1", msg_type_name="text",
                                content="[引用 x: [媒体消息]]", account_id="a1"))
            session.commit()

        # A picture message: registered even though no file was found yet.
        made = register_from_message(engine, message_id=1, msg_id=1001, chat_id="c1",
                                     kind=KIND_IMAGE, md5=MD5_42B, account_id="a1")
        ok &= made is True
        print(f"  {'✓' if made else '✗'} 图片消息 -> 建行")

        # Plain text: nothing to track.
        skipped = register_from_message(engine, message_id=2, msg_id=1002, chat_id="c1",
                                        account_id="a1")
        ok &= skipped is False
        print(f"  {'✓' if not skipped else '✗'} 纯文本 -> 跳过")

        # A reply that quotes a text message: still nothing to track.
        reply_text = register_from_message(engine, message_id=3, msg_id=1003,
                                           chat_id="c1", quote_ref="1002", account_id="a1")
        ok &= reply_text is False
        print(f"  {'✓' if not reply_text else '✗'} 引用文字 -> 跳过")

        # A reply that quotes the picture: tracked, pointing at the picture.
        reply_media = register_from_message(engine, message_id=4, msg_id=1004,
                                            chat_id="c1", quote_ref="1001",
                                            account_id="a1")
        ok &= reply_media is True
        print(f"  {'✓' if reply_media else '✗'} 引用图片 -> 建行")

        with Session(engine) as session:
            rows = session.scalars(select(MessageMedia).order_by(MessageMedia.id)).all()
            kinds = [(r.kind, r.status) for r in rows]
            quotes = [r.quote_ref for r in rows if r.kind == KIND_QUOTE_IMAGE]
        expected_kinds = [(KIND_IMAGE, STATUS_PENDING), (KIND_QUOTE_IMAGE, STATUS_PENDING)]
        good = kinds == expected_kinds and quotes == ["1001"]
        ok &= good
        print(f"  {'✓' if good else '✗'} 行内容 {kinds} 引用={quotes}")

        ok &= quoted_is_media(engine, "1001") is True
        ok &= quoted_is_media(engine, "1002") is False
        ok &= quoted_is_media(engine, "9999") is False
        print("  ✓ quoted_is_media 判定正确")
    finally:
        engine.dispose()
    return bool(ok)


def test_register_is_idempotent_and_type_tolerant() -> bool:
    """Re-registering updates clues instead of duplicating, and a raw SQL
    timestamp string is accepted (that string once broke 131 writes)."""
    print("\n[注册幂等 + 时间类型容错]")
    engine = _fresh_engine()
    ok = True
    try:
        register(engine, message_id=1, msg_id=1001, chat_id="c1", kind=KIND_IMAGE,
                 md5=MD5_42B, account_id="a1", created_at="2026-09-19 23:43:53.000000")
        register(engine, message_id=1, msg_id=1001, chat_id="c1", kind=KIND_IMAGE,
                 md5=MD5_46B, account_id="a1", created_at="2026-09-19 23:43:53.000000")
        with Session(engine) as session:
            rows = session.scalars(select(MessageMedia)).all()
        one_row = len(rows) == 1
        clue_updated = rows and rows[0].md5 == MD5_46B
        time_ok = rows and isinstance(rows[0].created_at, datetime)
        ok &= one_row and bool(clue_updated) and bool(time_ok)
        print(f"  {'✓' if one_row else '✗'} 只有一行（幂等）")
        print(f"  {'✓' if clue_updated else '✗'} 线索被更新为最新 md5")
        print(f"  {'✓' if time_ok else '✗'} 字符串时间被接受 {rows[0].created_at if rows else ''}")
    finally:
        engine.dispose()
    return bool(ok)


def test_resolution_fills_blank_only() -> bool:
    """Resolution writes media_path back, and never overwrites an existing one."""
    print("\n[解析与回写]")
    if not SAMPLE_IMAGE.exists():
        print(f"  SKIP (缺少素材 {SAMPLE_IMAGE.name})")
        return True
    engine = _fresh_engine()
    ok = True
    try:
        with Session(engine) as session:
            session.add(Message(id=1, msg_id=1001, chat_id="c1", msg_type_name="image",
                                content="[图片]", media_path=None, account_id="a1"))
            session.add(Message(id=2, msg_id=1002, chat_id="c1", msg_type_name="image",
                                content="[图片]", media_path="keep/me.jpg",
                                account_id="a1"))
            session.commit()

        register(engine, message_id=1, msg_id=1001, chat_id="c1",
                 kind=KIND_IMAGE, md5=MD5_42B, account_id="a1")
        local, status, error = None, "", ""
        with Session(engine) as session:
            row = session.scalars(select(MessageMedia)).first()
            local, status, error = resolve_once(row, engine=engine)
        good = bool(local) and status == STATUS_RESOLVED
        ok &= good
        print(f"  {'✓' if good else '✗'} 解析 -> {status} {local or error}")

        with Session(engine) as session:
            filled = session.get(Message, 1).media_path
        ok &= bool(filled)
        print(f"  {'✓' if filled else '✗'} 空 media_path 被回填")

        # A row that already had a path keeps it.
        register(engine, message_id=2, msg_id=1002, chat_id="c1",
                 kind=KIND_IMAGE, md5=MD5_42B, account_id="a1")
        with Session(engine) as session:
            row2 = session.scalars(
                select(MessageMedia).where(MessageMedia.message_id == 2)
            ).first()
            resolve_once(row2, engine=engine)
        with Session(engine) as session:
            kept = session.get(Message, 2).media_path
        ok &= kept == "keep/me.jpg"
        print(f"  {'✓' if kept == 'keep/me.jpg' else '✗'} 已有路径不被覆盖（{kept}）")
    finally:
        engine.dispose()
    return bool(ok)


def test_retry_state_machine() -> bool:
    """Pending rows retry with backoff, and give up instead of looping forever."""
    print("\n[重试状态机]")
    engine = _fresh_engine()
    ok = True
    try:
        # Unresolvable on purpose: a well-formed md5 with no file anywhere.
        register(engine, message_id=1, msg_id=1001, chat_id="c1", kind=KIND_IMAGE,
                 md5="0" * 31 + "1", account_id="a1")
        stats = resolve_pending(engine, limit=10, force=True)
        ok &= stats["tried"] == 1 and stats["still_pending"] == 1
        print(f"  ✓ 第一次尝试 -> {stats}")

        with Session(engine) as session:
            row = session.scalars(select(MessageMedia)).first()
            attempts, next_at, status = row.attempts, row.next_retry_at, row.status
        scheduled = next_at is not None and next_at > datetime.now()
        ok &= attempts == 1 and scheduled and status == STATUS_PENDING
        print(f"  {'✓' if scheduled else '✗'} 退避已排期 attempts={attempts}")

        # Without force, the backoff is respected.
        stats2 = resolve_pending(engine, limit=10)
        ok &= stats2["tried"] == 0
        print(f"  {'✓' if stats2['tried'] == 0 else '✗'} 未到期的行不会被重试")

        # Exhaust the schedule: the row must become missing, not retry forever.
        for _ in range(12):
            resolve_pending(engine, limit=10, force=True)
        with Session(engine) as session:
            row = session.scalars(select(MessageMedia)).first()
            final_status, final_next = row.status, row.next_retry_at
        gave_up = final_status == STATUS_MISSING and final_next is None
        ok &= gave_up
        print(f"  {'✓' if gave_up else '✗'} 重试用尽 -> {final_status}")
    finally:
        engine.dispose()
    return bool(ok)


def test_description_round_trip() -> bool:
    """A description stored on the row comes back, and reaches the transcript."""
    print("\n[描述落库与渲染]")
    if not SAMPLE_IMAGE.exists():
        print(f"  SKIP (缺少素材 {SAMPLE_IMAGE.name})")
        return True
    engine = _fresh_engine()
    ok = True
    try:
        from core.conversation.media import render_media_content

        with Session(engine) as session:
            session.add(Message(id=1, msg_id=1001, chat_id="c1", msg_type_name="image",
                                content="[图片]", account_id="a1"))
            session.commit()
        register(engine, message_id=1, msg_id=1001, chat_id="c1", kind=KIND_IMAGE,
                 md5=MD5_42B, account_id="a1",
                 created_at=datetime(2026, 9, 19, 23, 43, 53))
        # Through the real retry pass, which is what persists local_path onto
        # the row; resolve_once alone only reports the result.
        resolve_pending(engine, limit=10, force=True)
        with Session(engine) as session:
            row = session.scalars(select(MessageMedia)).first()
            row_path = str(row.local_path or "")
        ok &= bool(row_path)
        print(f"  {'✓' if row_path else '✗'} 行上记录了文件 {Path(row_path).name if row_path else ''}")

        stored = remember_description(engine, row_path, "一只猫在看电脑")
        ok &= stored >= 1
        print(f"  {'✓' if stored else '✗'} 描述写入 {stored} 行")

        path, description = lookup_description(engine, path=row_path)
        ok &= description == "一只猫在看电脑"
        print(f"  {'✓' if description else '✗'} 按路径查回: {description!r}")

        # A caller holding an absolute path still reaches the same row: the
        # stored path is however the resolver wrote it.
        by_name = remember_description(engine, str(SAMPLE_IMAGE), "换个说法")
        ok &= by_name >= 1
        print(f"  {'✓' if by_name else '✗'} 绝对路径也能命中同一行（{by_name}）")
        remember_description(engine, row_path, "一只猫在看电脑")

        # Found through the conversation + moment when the path is unknown.
        found = nearby_resolved_path(engine, "c1", datetime(2026, 9, 19, 23, 44, 10), 120)
        ok &= bool(found)
        print(f"  {'✓' if found else '✗'} 按会话+时间窗找到文件")

        rendered = render_media_content(
            "[图片]", chat_id="c1", created_at=datetime(2026, 9, 19, 23, 43, 53),
            engine=engine,
        )
        good = "[图片: 一只猫在看电脑]" in rendered
        ok &= good
        print(f"  {'✓' if good else '✗'} 渲染 -> {rendered}")

        # Nothing known: the placeholder must stay untouched.
        plain = render_media_content("[图片]", engine=engine)
        ok &= plain == "[图片]"
        print(f"  {'✓' if plain == '[图片]' else '✗'} 无描述时保持原样")
    finally:
        engine.dispose()
    return bool(ok)


def test_ingest_reads_only_real_wxmessage_fields() -> bool:
    """Every ``wx_msg.<attr>`` the ingest loop reads must exist on WxMessage.

    This is the check that was missing when ``wx_msg.msg_id`` -- a field that
    does not exist; the raw object calls it ``msg_svr_id`` -- was read inside
    the ingest batch.  The AttributeError aborted the whole batch, so a new
    message was never stored and she silently stopped answering.
    """
    print("\n[ingest 字段契约]")
    import re
    from dataclasses import fields

    from db.wx_schema import WxMessage

    source = (_ROOT / "db" / "sync_service.py").read_text(encoding="utf-8-sig")
    used = sorted(set(re.findall(r"wx_msg\.([A-Za-z_][A-Za-z0-9_]*)", source)))
    # Fields plus anything declared on the class -- ``is_processable`` is a
    # property, and only fields would call it unknown.
    known = {f.name for f in fields(WxMessage)}
    known |= {name for name in vars(WxMessage) if not name.startswith("_")}
    unknown = [name for name in used if name not in known]

    ok = not unknown
    print(f"  ✓ sync_service 读取 {len(used)} 个 wx_msg 字段")
    if unknown:
        print(f"  ✗ 不存在的字段: {unknown}")
    else:
        print("  ✓ 全部存在于 WxMessage")
    # The field the bug was about must be the one we now use.
    ok &= "msg_svr_id" in used and "msg_id" not in used
    print(f"  {'✓' if 'msg_id' not in used else '✗'} 没有再用不存在的 msg_id")
    return bool(ok)


def test_rows_are_ordered_by_message_time() -> bool:
    """Ordering is by the message's time, not by when the row was written.

    A backfill writes old pictures after new ones, so row id order is not time
    order.  The image tool used to trust id order and handed her a twelve-day-old
    picture when asked about the one just sent -- and she described it.
    """
    print("\n[按消息时间排序]")
    engine = _fresh_engine()
    ok = True
    try:
        # Written in the opposite order to their message times on purpose.
        register(engine, message_id=1, msg_id=1001, chat_id="c1", kind=KIND_IMAGE,
                 md5=MD5_42B, account_id="a1",
                 created_at=datetime(2026, 9, 8, 17, 52, 32))   # older message
        register(engine, message_id=2, msg_id=1002, chat_id="c1", kind=KIND_IMAGE,
                 md5=MD5_46B, account_id="a1",
                 created_at=datetime(2026, 9, 20, 0, 12, 35))   # newer message
        from core.conversation.media_registry import rows_for_chat

        rows = rows_for_chat(engine, "c1", limit=10)
        newest = rows[0]
        expected = newest.msg_id == 1002
        ok &= expected and len(rows) == 2
        print(f"  {'✓' if expected else '✗'} 最新一条是后来才写的旧消息吗: "
              f"{'否（正确）' if expected else '是（错误）'}")
        print(f"  ✓ 顺序: {[r.msg_id for r in rows]}")
    finally:
        engine.dispose()
    return bool(ok)


def test_fallback_refuses_a_stale_image() -> bool:
    """The image tool takes the picture belonging to the moment it is answering,
    and refuses a stale one instead of describing it."""
    print("\n[兜底只认本轮那张图]")
    engine = _fresh_engine()
    ok = True
    try:
        from datetime import timedelta

        from agents.tools.analysis import AnalyzeImageTool

        if not SAMPLE_IMAGE.exists():
            print(f"  SKIP (缺少素材 {SAMPLE_IMAGE.name})")
            return True

        with Session(engine) as session:
            session.add(Message(id=1, msg_id=1001, chat_id="c1", msg_type_name="image",
                                content="[图片]", account_id="a1"))
            session.commit()
        # The only picture in the chat is from 09-08.
        register(engine, message_id=1, msg_id=1001, chat_id="c1", kind=KIND_IMAGE,
                 md5=MD5_42B, account_id="a1",
                 created_at=datetime(2026, 9, 8, 17, 52, 32))
        resolve_pending(engine, limit=5, force=True)

        class _Ctx:
            def __init__(self, moment):
                self.extra = {"chat_id": "c1", "message_time": moment}

        # Monkeypatch the engine lookup so the tool reads the throwaway DB.
        import agents.tools.analysis as analysis_mod
        import core.bootstrap as bootstrap_mod

        real_get_repo = getattr(bootstrap_mod, "get_repo", None)
        try:
            bootstrap_mod.get_repo = lambda: type("R", (), {"engine": engine})()
            analysis_mod.Path  # noqa: B018 - keep the import honest

            near = AnalyzeImageTool()
            near.set_context(_Ctx(datetime(2026, 9, 8, 17, 53, 0)))
            got_near = near._latest_chat_image()
            far = AnalyzeImageTool()
            far.set_context(_Ctx(datetime(2026, 9, 20, 12, 0, 0)))
            got_far = far._latest_chat_image()
        finally:
            if real_get_repo is not None:
                bootstrap_mod.get_repo = real_get_repo

        picked = Path(got_near).name == Path(str(SAMPLE_IMAGE)).name
        refused = got_far == ""
        ok &= picked and refused
        print(f"  {'✓' if picked else '✗'} 同一时刻的图被采用: {Path(got_near).name if got_near else '(空)'}")
        print(f"  {'✓' if refused else '✗'} 隔了 12 天的图被拒绝: {got_far or '(空)'}")
    finally:
        engine.dispose()
    return bool(ok)


def test_quoted_sticker_wins_and_self_sent_is_ignored() -> bool:
    """The turn's quoted sticker beats any recent picture, and a picture she
    sent herself is never used as the fallback.

    Regression for the 2026-09-24 incident: P5's sticker was quoted, the
    registry row for it was still ``pending``, and the tool fell back to
    e302850b -- her *own* "嗯" sticker sent seven minutes earlier -- then told
    the group its description was wrong.  The quoted sticker's file existed in
    the emoji cache the whole time.
    """
    print("\n[引用表情包优先 / 不看她自己发的图]")
    engine = _fresh_engine()
    ok = True
    try:
        from agents.tools.analysis import AnalyzeImageTool

        if not SAMPLE_IMAGE.exists():
            print(f"  SKIP (缺少素材 {SAMPLE_IMAGE.name})")
            return True
        alt = _ROOT / "data" / "image_cache" / "44399fbdb2b7cf8f5f73d49a91491340.gif"
        moment = datetime(2026, 9, 8, 17, 53, 0)

        # 她自己在同一时刻发出的图：resolved 且文件存在，旧代码会选中它。
        register(engine, message_id=1, msg_id=1001, chat_id="c1", sender_id="a1",
                 kind=KIND_IMAGE, md5=MD5_46B, account_id="a1",
                 created_at=moment)
        with Session(engine) as session:
            row = session.scalar(
                select(MessageMedia).where(MessageMedia.msg_id == 1001)
            )
            row.local_path = str(SAMPLE_IMAGE)
            row.status = STATUS_RESOLVED
            session.commit()

        import core.bootstrap as bootstrap_mod
        import core.preprocess.emoji_recognition as emoji_mod

        real_get_repo = getattr(bootstrap_mod, "get_repo", None)
        real_find = getattr(emoji_mod, "find_emoji_image_path", None)

        class _Ctx:
            def __init__(self, extra):
                self.extra = extra

        quoted_extra = {
            "chat_id": "c1", "account_id": "a1", "message_time": moment,
            "quoted_sticker_id": "afd8b92ddf249d6a90b8a327663e9882",
        }

        try:
            bootstrap_mod.get_repo = lambda: type("R", (), {"engine": engine})()
            emoji_mod.find_emoji_image_path = lambda md5: str(SAMPLE_IMAGE)

            quoted = AnalyzeImageTool()
            quoted.set_context(_Ctx(dict(quoted_extra)))
            uri, source, err = quoted._resolve_image("")
            got_quoted = bool(uri) and Path(source).name == SAMPLE_IMAGE.name and not err
            ok &= got_quoted
            print(f"  {'✓' if got_quoted else '✗'} 引用表情包拿到图片: "
                  f"{Path(source).name if source else '(空)'}")

            # 新契约 TurnReferent 也必须走同一条路：锚里的 quoted 优先。
            with_ref = AnalyzeImageTool()
            with_ref.set_context(_Ctx({
                "chat_id": "c1", "account_id": "a1", "message_time": moment,
                "referent": {
                    "chat_id": "c1", "message_id": "m1",
                    "quoted_message_id": "m0", "quoted_author": "P5",
                    "media": [{
                        "role": "quoted", "kind": "sticker",
                        "md5": "afd8b92ddf249d6a90b8a327663e9882",
                        "path": "", "sender_id": "", "sender_name": "P5",
                        "is_self": False,
                    }],
                },
            }))
            uri_r, source_r, err_r = with_ref._resolve_image("")
            ref_ok = bool(uri_r) and Path(source_r).name == SAMPLE_IMAGE.name and not err_r
            ok &= ref_ok
            print(f"  {'✓' if ref_ok else '✗'} referent 锚同样优先引用")

            # 引用拿不到文件时必须拒绝，而不是退回"最近一张"。
            emoji_mod.find_emoji_image_path = lambda md5: ""
            refused = AnalyzeImageTool()
            refused.set_context(_Ctx(dict(quoted_extra)))
            uri2, source2, err2 = refused._resolve_image("")
            no_substitute = (not uri2) and bool(err2)
            ok &= no_substitute
            print(f"  {'✓' if no_substitute else '✗'} 引用解析不到时拒绝而非顶替")

            # 锚解析不到同样必须诚实失败（同一条不顶替红线）。
            emoji_mod.find_emoji_image_path = lambda md5: ""
            ref_refused = AnalyzeImageTool()
            ref_refused.set_context(_Ctx({
                "chat_id": "c1", "account_id": "a1", "message_time": moment,
                "referent": {
                    "chat_id": "c1", "message_id": "m1",
                    "media": [{
                        "role": "quoted", "kind": "sticker",
                        "md5": "afd8b92ddf249d6a90b8a327663e9882",
                        "path": "", "sender_id": "", "sender_name": "P5",
                        "is_self": False,
                    }],
                },
            }))
            uri_r2, source_r2, err_r2 = ref_refused._resolve_image("")
            ref_no_sub = (not uri_r2) and bool(err_r2)
            ok &= ref_no_sub
            print(f"  {'✓' if ref_no_sub else '✗'} referent 解析不到时拒绝而非顶替")

            # 没有引用时，兜底也不能选她自己发出的那张。
            plain = AnalyzeImageTool()
            plain.set_context(_Ctx({
                "chat_id": "c1", "account_id": "a1", "message_time": moment,
            }))
            got = plain._latest_chat_image()
            ignored_self = got == ""
            ok &= ignored_self
            print(f"  {'✓' if ignored_self else '✗'} 她自己发的图不进兜底: "
                  f"{Path(got).name if got else '(空)'}")

            # 同一时刻对方发的图仍然可以被兜底选中。
            if alt.exists():
                register(engine, message_id=2, msg_id=1002, chat_id="c1",
                         sender_id="u2", kind=KIND_IMAGE, md5=MD5_45B,
                         account_id="a1", created_at=moment)
                with Session(engine) as session:
                    row = session.scalar(
                        select(MessageMedia).where(MessageMedia.msg_id == 1002)
                    )
                    row.local_path = str(alt)
                    row.status = STATUS_RESOLVED
                    session.commit()
                peer = AnalyzeImageTool()
                peer.set_context(_Ctx({
                    "chat_id": "c1", "account_id": "a1", "message_time": moment,
                }))
                got2 = peer._latest_chat_image()
                peer_picked = Path(got2).name == alt.name
                ok &= peer_picked
                print(f"  {'✓' if peer_picked else '✗'} 对方发的图仍可用: "
                      f"{Path(got2).name if got2 else '(空)'}")
            else:
                print("  SKIP (缺少第二张素材)")
        finally:
            if real_get_repo is not None:
                bootstrap_mod.get_repo = real_get_repo
            if real_find is not None:
                emoji_mod.find_emoji_image_path = real_find
    finally:
        engine.dispose()
    return bool(ok)


def main() -> bool:
    print("=" * 60)
    print("media registry checks")
    print("=" * 60)
    tests = [
        test_session_dir_matches_wechat_layout,
        test_md5_from_every_payload_variant,
        test_registration_rules,
        test_register_is_idempotent_and_type_tolerant,
        test_resolution_fills_blank_only,
        test_retry_state_machine,
        test_description_round_trip,
        test_ingest_reads_only_real_wxmessage_fields,
        test_rows_are_ordered_by_message_time,
        test_fallback_refuses_a_stale_image,
        test_quoted_sticker_wins_and_self_sent_is_ignored,
    ]
    results = []
    try:
        for test in tests:
            try:
                results.append((test.__name__, bool(test())))
            except Exception:  # pragma: no cover - diagnostics only
                import traceback

                traceback.print_exc()
                results.append((test.__name__, False))
    finally:
        _cleanup()

    print("\n" + "=" * 60)
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
    failed = [n for n, ok in results if not ok]
    print(f"media registry checks: {len(results) - len(failed)}/{len(results)} passed")
    print("=" * 60)
    return not failed


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(0 if main() else 1)
