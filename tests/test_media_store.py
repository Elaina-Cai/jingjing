"""message_media 基质：抽取器 + 落库幂等（task-26，纯本地、无网络）。"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ingestion import media_store as ms  # noqa: E402

MEDIA_ENV = "YINYUE_FEATURE_MEDIA_STORE"


def _store(value: str | None) -> None:
    if value is None:
        os.environ.pop(MEDIA_ENV, None)
    else:
        os.environ[MEDIA_ENV] = value


def check(name: str, ok: bool) -> bool:
    print(("  PASS " if ok else "  FAIL ") + name)
    return bool(ok)


def _workdir() -> pathlib.Path:
    base = ROOT / ".test_tmp"
    base.mkdir(parents=True, exist_ok=True)
    path = pathlib.Path(tempfile.mkdtemp(prefix="media_store_test_", dir=str(base)))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(path: pathlib.Path, data: str | bytes) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    return path


def _zip_with(path: pathlib.Path, members: dict[str, str]) -> pathlib.Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return path


def test_extract_text_files_and_cap() -> bool:
    work = _workdir()
    try:
        note = _write(work / "note.md", "# 标题\n电桥实验记录")
        result = ms.extract_media_text(note)
        ok = check("md 直读", result["status"] == "ok" and "电桥实验记录" in result["text"])
        ok = check("size_bytes 有值", result["size_bytes"] > 0) and ok
        big = _write(work / "big.txt", "x" * (ms.READ_MAX_BYTES + 5000))
        capped = ms.extract_media_text(big)
        ok = check("大文件截断", capped["status"] == "ok" and len(capped["text"]) == ms.TEXT_EXCERPT_MAX) and ok
        return ok
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_extract_office_and_zip() -> bool:
    work = _workdir()
    try:
        docx = _zip_with(
            work / "作业.docx",
            {
                "word/document.xml": (
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    "<w:body><w:p><w:r><w:t>第一题 求电场强度</w:t></w:r></w:p></w:body></w:document>"
                )
            },
        )
        result = ms.extract_media_text(docx)
        ok = check("docx 文本层", result["status"] == "ok" and "第一题" in result["text"])
        xlsx = _zip_with(
            work / "课程表.xlsx",
            {"xl/sharedStrings.xml": "<sst><si><t>高等数学</t></si></sst>"},
        )
        result = ms.extract_media_text(xlsx)
        ok = check("xlsx 共享串", result["status"] == "ok" and "高等数学" in result["text"]) and ok
        pptx = _zip_with(
            work / "课件.pptx",
            {
                "ppt/slides/slide1.xml": (
                    '<p:sld xmlns:p="p" xmlns:a="a"><p:cSld><p:spTree><p:sp><p:txBody>'
                    "<a:p><a:r><a:t>戴维南定理</a:t></a:r></a:p></p:txBody></p:sp></p:spTree>"
                    "</p:cSld></p:sld>"
                )
            },
        )
        result = ms.extract_media_text(pptx)
        ok = check("pptx 幻灯片", result["status"] == "ok" and "戴维南" in result["text"]) and ok
        archive = _zip_with(work / "大学物理实验.zip", {"readme.txt": "hi", "data/raw.csv": "1,2"})
        result = ms.extract_media_text(archive)
        ok = check(
            "zip 列目录",
            result["status"] == "ok" and "readme.txt" in result["text"] and "raw.csv" in result["text"],
        ) and ok
        return ok
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_extract_failures_are_explicit() -> bool:
    work = _workdir()
    try:
        missing = ms.extract_media_text(work / "nope.pdf")
        ok = check("路径解析到但文件不在 -> file gone", "file gone" in missing["reason"])
        unresolved = ms.extract_media_text(None)
        ok = check("路径未解析 -> not resolved", "not resolved" in unresolved["reason"]) and ok
        ok = check("缺文件 status=missing", missing["status"] == "missing" and bool(missing["reason"]))
        empty = _write(work / "blank.docx", b"")
        result = ms.extract_media_text(empty)
        ok = check("坏 docx 记 unsupported", result["status"] == "unsupported" and bool(result["reason"])) and ok
        unknown = _write(work / "raw.bin", b"\x00\x01")
        result = ms.extract_media_text(unknown)
        ok = check("无抽取器记 unsupported", result["status"] == "unsupported" and "no extractor" in result["reason"]) and ok
        image = _write(work / "pic.png", b"\x89PNG\r\n")
        result = ms.extract_media_text(image)
        ok = check("图片算 ok 无正文", result["status"] == "ok" and result["text"] == "") and ok
        return ok
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _media_message(chat_id: str, msg_id: int, name: str) -> dict:
    return {
        "chat_id": chat_id,
        "msg_id": msg_id,
        "sender_id": "wxid_sender",
        "content": "[文件]",
        "msg_type": "file",
        "media_path": "",
        "original_name": name,
    }


def test_register_rows_off_and_on_are_idempotent() -> bool:
    work = _workdir()
    try:
        db_path = work / "meta.db"
        from db import migrations

        migrations.run_migrations(db_path)
        from sqlalchemy import create_engine, text as sql_text

        engine = create_engine(f"sqlite:///{db_path.as_posix()}")
        chat = "media_store_test@chatroom"
        rows = [_media_message(chat, 101, "作业.pdf"), _media_message(chat, 102, "课表.xlsx")]

        _store("off")
        ok = check("off 不写库", ms.register_media_rows(rows, platform="wechat", account_id="bot", engine=engine) == 0)
        with engine.connect() as conn:
            count = conn.execute(sql_text("select count(*) from message_media")).scalar()
        ok = check("off 零行", int(count or 0) == 0) and ok

        _store("on")
        written = ms.register_media_rows(
            rows,
            platform="wechat",
            account_id="bot",
            event_ids=["e101", "e102"],
            engine=engine,
        )
        ok = check("on 写两行", written == 2) and ok
        again = ms.register_media_rows(
            rows,
            platform="wechat",
            account_id="bot",
            event_ids=["e101", "e102"],
            engine=engine,
        )
        ok = check("幂等跳过", again == 0) and ok
        with engine.connect() as conn:
            stored = conn.execute(
                sql_text(
                    "select original_name, kind, event_id, status, last_error from message_media"
                    " where chat_id = :cid order by msg_id"
                ),
                {"cid": chat},
            ).fetchall()
        ok = check("两行都在", len(stored) == 2) and ok
        ok = check("附件名落库", [row[0] for row in stored] == ["作业.pdf", "课表.xlsx"]) and ok
        ok = check("event_id 落库", [row[2] for row in stored] == ["e101", "e102"]) and ok
        ok = check(
            "抽取失败显式记 status/reason",
            [row[3] for row in stored] == ["missing", "missing"]
            and all(str(row[4] or "").strip() for row in stored),
        ) and ok
        capped = ms.register_media_rows(
            [_media_message(chat, 200 + index, "x%d.pdf" % index) for index in range(5)],
            platform="wechat",
            account_id="bot",
            engine=engine,
            max_rows=2,
        )
        ok = check("单批上限", capped == 2) and ok
        _store(None)
        return ok
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_mime_falls_back_to_suffix() -> bool:
    work = _workdir()
    try:
        image = _write(work / "pic.jpg", b"\xff\xd8\xff")
        result = ms.extract_media_text(image)
        ok = check("图片 mime 按扩展名兜底", result["mime"] == "image/jpeg") and result["status"] == "ok"
        pdf = _write(work / "a.PDF", b"%PDF-1.4\n")
        ok = check("大写后缀也认得", ms.extract_media_text(pdf)["mime"] == "application/pdf") and ok
        ok = check("无扩展名不给 mime", ms.mime_of_row({"original_name": "a.bin"}) == "") and ok
        ok = check("路径未解析时按附件名兜底", ms.mime_of_row({"original_name": "b.xlsx"}) == ms.MIME_BY_SUFFIX["xlsx"]) and ok
        ok = check(
            "采集侧声明优先",
            ms.mime_of_row({"media_mime": "image/heic", "original_name": "c.jpg"}) == "image/heic",
        ) and ok
        ok = check("None 路径不炸", ms.mime_of_row({}) == "") and ok
        return ok
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_register_writes_mime_for_image() -> bool:
    work = _workdir()
    try:
        db_path = work / "meta.db"
        from db import migrations

        migrations.run_migrations(db_path)
        from sqlalchemy import create_engine, text as sql_text

        engine = create_engine(f"sqlite:///{db_path.as_posix()}")
        chat = "media_mime_test@chatroom"
        image = _write(work / "c34_b.jpg", b"\xff\xd8\xff")
        row = {
            "chat_id": chat,
            "msg_id": 301,
            "sender_id": "wxid_sender",
            "content": "[图片]",
            "msg_type": "image",
            "media_path": str(image),
            "original_name": "c34_b.jpg",
            "media_mime": "",
        }
        _store("on")
        written = ms.register_media_rows(
            [row], platform="wechat", account_id="bot", event_ids=["e301"], engine=engine,
        )
        ok = check("图片写一行", written == 1)
        with engine.connect() as conn:
            stored = conn.execute(
                sql_text("select status, mime, size_bytes, original_name from message_media where chat_id = :cid"),
                {"cid": chat},
            ).fetchone()
        ok = check("status=ok", stored is not None and stored[0] == "ok") and ok
        ok = check("mime 落库不再为空", stored is not None and stored[1] == "image/jpeg") and ok
        ok = check("size_bytes 有值", stored is not None and int(stored[2] or 0) == 3) and ok
        _store(None)
        return ok
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_kind_and_name_helpers() -> bool:
    ok = check("占位符 kind", ms.kind_of_row({"content": "[图片]", "msg_type": ""}) == "image")
    ok = check("文件占位符", ms.kind_of_row({"content": "[文件]", "msg_type": ""}) == "file") and ok
    ok = check(
        "名字从正文回退",
        ms.original_name_from_row({"content": "大学物理实验.zip", "msg_type": "file"}) == "大学物理实验.zip",
    ) and ok
    ok = check(
        "显式名字优先",
        ms.original_name_from_row(
            {"content": "[文件]", "original_name": "物理作业.pdf", "msg_type": "file"}
        )
        == "物理作业.pdf",
    ) and ok
    return ok


def _bridge_dbs(work: pathlib.Path):
    """建 meta 库（跑迁移）与账号库（手工建 message_media 三列），返回两个 engine。"""
    from db import migrations
    from sqlalchemy import create_engine, text as sql_text

    meta_path = work / "meta.db"
    migrations.run_migrations(meta_path)
    meta_engine = create_engine(f"sqlite:///{meta_path.as_posix()}")

    account_path = work / "account.db"
    account_engine = create_engine(f"sqlite:///{account_path.as_posix()}")
    with account_engine.begin() as conn:
        conn.execute(
            sql_text(
                "CREATE TABLE message_media ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER, chat_id TEXT,"
                " kind TEXT, status TEXT, local_path TEXT, resolved_at DATETIME)"
            )
        )
    return meta_engine, account_engine


def _insert_meta(engine, **values) -> None:
    from sqlalchemy import text as sql_text

    payload = {
        "msg_id": 0,
        "platform": "wechat",
        "account_id": "bot",
        "chat_id": "bridge_test@chatroom",
        "kind": "image",
        "status": "missing",
        "md5": "",
        "local_path": "",
        "text_excerpt": "",
        "mime": "",
        "size_bytes": 0,
        "meta_json": "{}",
        "created_at": "2026-10-17 15:00:00",
    }
    payload.update(values)
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO message_media (msg_id, platform, account_id, chat_id, kind,"
                " status, md5, local_path, text_excerpt, mime, size_bytes, meta_json,"
                " created_at) VALUES (:msg_id, :platform, :account_id, :chat_id, :kind,"
                " :status, :md5, :local_path, :text_excerpt, :mime, :size_bytes,"
                " :meta_json, :created_at)"
            ),
            payload,
        )


def _insert_account(engine, **values) -> None:
    from sqlalchemy import text as sql_text

    payload = {
        "msg_id": 0,
        "chat_id": "bridge_test@chatroom",
        "kind": "image",
        "status": "resolved",
        "local_path": "",
        "resolved_at": "2026-10-17 15:40:00",
    }
    payload.update(values)
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO message_media (msg_id, chat_id, kind, status, local_path,"
                " resolved_at) VALUES (:msg_id, :chat_id, :kind, :status, :local_path,"
                " :resolved_at)"
            ),
            payload,
        )


def test_backfill_bridges_missing_row_only() -> bool:
    work = _workdir()
    try:
        import json

        from sqlalchemy import text as sql_text

        meta_engine, account_engine = _bridge_dbs(work)
        chat = "bridge_test@chatroom"
        _insert_meta(meta_engine, msg_id=111, chat_id=chat, kind="emoji", status="missing")
        _insert_meta(
            meta_engine,
            msg_id=222,
            chat_id=chat,
            kind="image",
            status="ok",
            md5="keep",
            local_path="/tmp/keep.jpg",
            text_excerpt="keep-me",
            mime="image/jpeg",
        )
        # bridge 两份账号行：一个表情包（resolved 成 png，无文本层）、一个文件（可抽文本）
        emoji_png = _write(work / "emoji.png", b"\x89PNG\r\n")
        note = _write(work / "作业.txt", "电桥实验记录 双臂桥")
        _insert_account(account_engine, msg_id=111, chat_id=chat, kind="emoji", local_path=str(emoji_png))
        _insert_meta(meta_engine, msg_id=333, chat_id=chat, kind="file", status="missing")
        _insert_account(account_engine, msg_id=333, chat_id=chat, kind="file", local_path=str(note))

        _store("on")
        report = ms.backfill_media_from_account(
            account_engine,
            platform="wechat",
            account_id="bot",
            limit=40,
            meta_engine=meta_engine,
        )
        ok = check("桥报告 rows=2", report["rows"] == 2)
        ok = check("桥报告 updated=2", report["updated"] == 2) and ok
        ok = check("桥报告 failed=0", report["failed"] == 0) and ok
        with meta_engine.connect() as conn:
            bridged = conn.execute(
                sql_text("select status, mime, size_bytes, md5, text_excerpt, meta_json,"
                         " local_path, original_name from message_media where msg_id = 111")
            ).fetchone()
            note_row = conn.execute(
                sql_text("select status, mime, text_excerpt, meta_json, original_name"
                         " from message_media where msg_id = 333")
            ).fetchone()
            kept = conn.execute(
                sql_text("select status, text_excerpt, mime from message_media where msg_id = 222")
            ).fetchone()
        ok = check("表情包 missing 行补成 ok", bridged is not None and bridged[0] == "ok") and ok
        ok = check("表情包 mime 按后缀", bridged is not None and bridged[1] == "image/png") and ok
        ok = check("size 重算", bridged is not None and int(bridged[2] or 0) > 0) and ok
        ok = check("md5 重算", bridged is not None and len(str(bridged[3])) == 32) and ok
        ok = check("无文本层 reason 落 meta", bridged is not None and "no text layer" in str(bridged[5])) and ok
        ok = check("meta_json 标记来源", bridged is not None and "account_db" in str(bridged[5])) and ok
        ok = check("文本件补成 ok", note_row is not None and note_row[0] == "ok") and ok
        ok = check("文本件 mime", note_row is not None and note_row[1] == "text/plain") and ok
        ok = check("摘要重算", note_row is not None and "电桥实验记录" in str(note_row[2])) and ok
        ok = check("ok 行没被覆盖", kept is not None and kept[1] == "keep-me" and kept[0] == "ok") and ok
        ok = check("空 original_name 用账号路径补", bridged is not None and bridged[7] == "emoji.png") and ok
        ok = check("文本件名字补齐", note_row is not None and note_row[4] == "作业.txt") and ok
        again = ms.backfill_media_from_account(
            account_engine,
            platform="wechat",
            account_id="bot",
            limit=40,
            meta_engine=meta_engine,
        )
        ok = check("重复回填 updated=0（幂等）", again["updated"] == 0 and again["rows"] == 2) and ok
        _store(None)
        return ok
    finally:
        _store(None)
        shutil.rmtree(work, ignore_errors=True)


def test_backfill_off_and_bad_path() -> bool:
    work = _workdir()
    try:
        meta_engine, account_engine = _bridge_dbs(work)
        chat = "bridge_test@chatroom"
        _insert_meta(meta_engine, msg_id=333, chat_id=chat, kind="image", status="missing")
        _insert_account(account_engine, msg_id=333, chat_id=chat, local_path=str(work / "gone.jpg"))

        _store("off")
        report = ms.backfill_media_from_account(
            account_engine, platform="wechat", account_id="bot", meta_engine=meta_engine,
        )
        ok = check("off 档零动作", report == {"rows": 0, "updated": 0, "extracted": 0, "failed": 0})

        _store("on")
        report = ms.backfill_media_from_account(
            account_engine, platform="wechat", account_id="bot", meta_engine=meta_engine,
        )
        ok = check("文件不在时只记 failed", report["rows"] == 1 and report["failed"] == 1) and ok
        ok = check("文件不在时不更新", report["updated"] == 0) and ok
        from sqlalchemy import text as sql_text

        with meta_engine.connect() as conn:
            row = dict(conn.execute(
                sql_text("SELECT status, last_error FROM message_media WHERE msg_id = 333")
            ).one()._mapping)
        ok = check("文件不在时仍留在 missing", row["status"] == "missing") and ok
        ok = check("文件不在时 last_error 有归因", "file gone" in str(row["last_error"])) and ok
        _store(None)
        return ok
    finally:
        _store(None)
        shutil.rmtree(work, ignore_errors=True)


def main() -> bool:
    names = sorted(name for name in globals() if name.startswith("test_"))
    ok = True
    for name in names:
        fn = globals()[name]
        try:
            result = bool(fn())
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
            result = False
        print(("ok   " if result else "FAIL ") + name)
        ok = ok and result
    print(f"media store: {len(names)} tests, {'all passed' if ok else 'FAILED'}")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
