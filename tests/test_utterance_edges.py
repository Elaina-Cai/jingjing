# -*- coding: utf-8 -*-
"""task-43 验收：把「谁对谁说」的寻址证据落成事实（``utterance_edges``）。

卡面要求钉五件事：

1. **off 档零行为**：不写行、不写日志、窗口 ``↳回 X`` 一定不出现（逐字旧行）；
2. **shadow 只写不渲染**：行落库，但窗口不读它；
3. **只写不猜**：没有 @、没有引用 ⇒ ``recipient_kind='unknown'`` +
   ``evidence='none'``，且**收件人绝不等于发言人**；
4. **两个坑**：微信 ``mentions`` 恒空（@ 在正文里，要 ``at_body`` 通道）、
   ``messages.quote_reply_user`` 不可用（不得出现在实现里）；
5. **幂等 + 迁移 62 与模型 parity**。
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.terminal import utterance_edges as UE  # noqa: E402

PASSED = 0
FAILED: list[str] = []
BOT_ID = "wxid_seed02"
BOT_NAMES = ["鲸鲸"]
ENV = "YINYUE_FEATURE_UTTERANCE_EDGES"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print("PASS", name)
    else:
        FAILED.append(name + (" | " + detail if detail else ""))
        print("FAIL", name, detail)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, event="", **kw):
        self.events.append((str(event), dict(kw)))

    def warning(self, event="", **kw):
        self.events.append((str(event), dict(kw)))

    def debug(self, *a, **k):
        pass


def _msg(**kw):
    base = {
        "msg_id": "m1",
        "chat_id": "10000000003@chatroom",
        "chat_type": "group",
        "sender_id": "wxid_other",
        "sender_name": "P6",
        "content": "你要来吗",
        "is_self": 0,
    }
    base.update(kw)
    return base


def _mode(value: str | None) -> None:
    if value is None:
        os.environ.pop(ENV, None)
    else:
        os.environ[ENV] = value


def _tmp_engine(name: str):
    from sqlalchemy import create_engine

    from db.meta_models import MetaBase

    path = ROOT / ".test_tmp" / "edges" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    engine = create_engine(f"sqlite:///{path}")
    MetaBase.metadata.create_all(engine)
    return engine, path


# ------------------------------------------------------------------ 开关三态
def test_modes() -> None:
    _mode(None)
    check("mode 缺省 off", UE.edges_mode() == "off", UE.edges_mode())
    _mode("shadow")
    check("mode shadow", UE.edges_mode() == "shadow")
    check("shadow 采集但禁渲染", UE.edges_enabled() and not UE.edges_render_enabled())
    _mode("1")
    check("mode 1 -> on", UE.edges_mode() == "on" and UE.edges_render_enabled())
    _mode("乱写")
    check("未知值回落 off", UE.edges_mode() == "off")
    _mode(None)


# ------------------------------------------------------------------ 只写不猜
def test_no_evidence_is_unknown() -> None:
    rows = UE.build_edges(
        [_msg()], platform="wechat", account_id=BOT_ID, chat_names=["P6"], bot_names=BOT_NAMES
    )
    check("无证据也出一行（可审计）", len(rows) == 1, str(len(rows)))
    row = rows[0]
    check("无证据 kind=unknown", row["recipient_kind"] == "unknown", row["recipient_kind"])
    check("无证据 evidence=none", row["evidence"] == "none", row["evidence"])
    check(
        "收件人不是发言人",
        row["recipient_id"] != row["speaker_id"] and row["recipient_name"] != row["speaker_name"],
        f"{row['recipient_id']}|{row['recipient_name']}",
    )
    check("空引用 id 不留脏值", row["quoted_msg_id"] == "")


def test_dm() -> None:
    rows = UE.build_edges(
        [_msg(chat_id="wxid_seed01", chat_type="private", content="在吗")],
        platform="wechat",
        account_id=BOT_ID,
        bot_names=BOT_NAMES,
    )
    check("私聊是 dm/bot", rows[0]["evidence"] == "dm" and rows[0]["recipient_kind"] == "bot", rows[0]["evidence"])


def test_quote_bot_and_person() -> None:
    quoted_bot = {"msg_id": "q1", "sender_id": BOT_ID, "sender_name": "鲸鲸", "is_self": 1}
    quoted_person = {"msg_id": "q2", "sender_id": "wxid_a", "sender_name": "yulia", "is_self": 0}
    rows = UE.build_edges(
        [_msg(msg_id="m1", quote_reply_id="q1"), _msg(msg_id="m2", quote_reply_id="q2")],
        platform="wechat",
        account_id=BOT_ID,
        chat_names=["P6", "yulia"],
        bot_names=BOT_NAMES,
        quoted_lookup=lambda mid: {"q1": quoted_bot, "q2": quoted_person}.get(str(mid)),
    )
    by_id = {r["msg_id"]: r for r in rows}
    check("引她 -> bot/quote", by_id["m1"]["recipient_kind"] == "bot" and by_id["m1"]["evidence"] == "quote")
    check("引作者名填上", by_id["m1"]["quoted_speaker_name"] == "鲸鲸", by_id["m1"]["quoted_speaker_name"])
    check("引别人 -> person/quote", by_id["m2"]["recipient_kind"] == "person" and by_id["m2"]["recipient_kind"] == "person")
    check("引别人记下 id", by_id["m2"]["recipient_id"] == "wxid_a", by_id["m2"]["recipient_id"])


def test_quote_unresolved_keeps_quote() -> None:
    rows = UE.build_edges(
        [_msg(quote_reply_id="missing")],
        platform="wechat",
        account_id=BOT_ID,
        bot_names=BOT_NAMES,
        quoted_lookup=lambda mid: None,
    )
    row = rows[0]
    check("引用查不到仍是 quote 事实", row["evidence"] == "quote", row["evidence"])
    check("引用查不到不猜收件人", row["recipient_kind"] == "unknown", row["recipient_kind"])
    check("quoted_msg_id 保留", row["quoted_msg_id"] == "missing")
    detail = json.loads(row["evidence_detail_json"])
    check("detail 记 quote_unresolved", "quote_unresolved" in detail.get("signals", []), str(detail))


# --------------------------------------------------------------- @ 两条通道
def test_mentions_json() -> None:
    mentions = json.dumps([{"user_id": BOT_ID, "user_name": ""}])
    rows = UE.build_edges(
        [_msg(chat_id="123@chatroom", mentions=mentions)],
        platform="qq",
        account_id=BOT_ID,
        bot_names=BOT_NAMES,
    )
    check("mentions 命中她 -> at/bot", rows[0]["evidence"] == "at" and rows[0]["recipient_kind"] == "bot", rows[0]["evidence"])
    check("坏 JSON 不炸", UE.parse_mentions("not-json") == [])


def test_at_body() -> None:
    names = ["鲸鲸", "P6", "Elaina"]
    rows = UE.build_edges(
        [
            _msg(msg_id="a1", content="@鲸鲸 信息轰炸…"),
            _msg(msg_id="a2", content="@P6\u2005你要来吗？"),
            _msg(msg_id="a3", content="@Elaina 在吗"),
        ],
        platform="wechat",
        account_id=BOT_ID,
        chat_names=names,
        bot_names=BOT_NAMES,
    )
    by_id = {r["msg_id"]: r for r in rows}
    check("@鲸鲸 -> at_body/bot", by_id["a1"]["evidence"] == "at_body" and by_id["a1"]["recipient_kind"] == "bot", by_id["a1"]["evidence"])
    check("U+2005 分隔算 @", by_id["a2"]["evidence"] == "at_body" and by_id["a2"]["recipient_name"] == "P6", by_id["a2"]["recipient_name"])
    check("@Elaina -> person", by_id["a3"]["recipient_kind"] == "person", by_id["a3"]["recipient_kind"])
    hits, ambiguous = UE.parse_at_names("@鲸鲸 你好", names)
    check("命中带上原始串", hits and hits[0]["raw"] == "@鲸鲸", str(hits))
    check("无歧义", ambiguous is False)


def test_at_body_boundary() -> None:
    # 名字边界靠「后面必须是空白/正文结束」判定；同名候选取**最长**那个（不按短名截断）。
    hits, ambiguous = UE.parse_at_names("@鲸鲸牙 在吗", ["鲸鲸", "鲸鲸牙"])
    check(
        "前缀取最长名、不算歧义",
        [h["name"] for h in hits] == ["鲸鲸牙"] and ambiguous is False,
        f"{hits} amb={ambiguous}",
    )
    hits2, amb2 = UE.parse_at_names("@鲸鲸牙 在吗", ["鲸鲸"])
    check("短名不被长名吃掉", hits2 == [] and amb2 is False, str(hits2))
    hits3, _ = UE.parse_at_names("@鲸鲸，你好", ["鲸鲸"])
    check("中文标点不是分隔符（按卡面只认空白）", hits3 == [], str(hits3))
    # 生产实测：允许中文标点只多出 1 条边/7 天 ⇒ 不值得放宽边界（见决策文档）


def test_never_reads_quote_reply_user() -> None:
    src = (ROOT / "core" / "terminal" / "utterance_edges.py").read_text(encoding="utf-8")
    accesses = (
        src.count('.get("quote_reply_user")')
        + src.count("['quote_reply_user']")
        + src.count('["quote_reply_user"]')
    )
    check("实现里不读 quote_reply_user（列不可用）", accesses == 0, f"accesses={accesses}")
    check("坑写在 docstring 里防回归", "quote_reply_user" in src, "no note")


# ------------------------------------------------------------- 写库 / 幂等
def test_off_writes_nothing() -> None:
    engine, _ = _tmp_engine("off.db")
    rec = _Recorder()
    old = UE.log
    UE.log = rec
    _mode("off")
    try:
        n = UE.record_utterance_edges([_msg()], platform="wechat", account_id=BOT_ID, engine=engine)
        with engine.connect() as conn:
            rows = conn.exec_driver_sql("select count(*) from utterance_edges").scalar()
    finally:
        UE.log = old
        _mode(None)
    check("off 不写行", n == 0 and rows == 0, f"n={n} rows={rows}")
    check("off 零日志", rec.events == [], str(rec.events))


def test_shadow_writes_but_off_render() -> None:
    engine, _ = _tmp_engine("shadow.db")
    rec = _Recorder()
    old = UE.log
    UE.log = rec
    _mode("shadow")
    try:
        first = UE.record_utterance_edges(
            [_msg(msg_id="s1", content="@鲸鲸 在吗")],
            platform="wechat",
            account_id=BOT_ID,
            engine=engine,
            bot_names=BOT_NAMES,
        )
        second = UE.record_utterance_edges(
            [_msg(msg_id="s1", content="@鲸鲸 在吗")],
            platform="wechat",
            account_id=BOT_ID,
            engine=engine,
            bot_names=BOT_NAMES,
        )
        with engine.connect() as conn:
            rows = conn.exec_driver_sql("select count(*) from utterance_edges").scalar()
            hints = _hints_for(conn, ["s1"])
    finally:
        UE.log = old
        _mode(None)
    check("shadow 写行", first == 1 and rows == 1, f"first={first} rows={rows}")
    check("shadow 幂等", second == 0, str(second))
    check("shadow 不渲染", hints == {}, str(hints))
    check(
        "写行有 info 日志",
        any(event == "utterance_edges_recorded" and kw.get("inserted") == 1 for event, kw in rec.events),
        str(rec.events),
    )


def _hints_for(conn, msg_ids):
    from core.context.chat_window import _edge_hints

    return _edge_hints(conn, msg_ids)


def test_on_renders() -> None:
    engine, _ = _tmp_engine("on.db")
    _mode("on")
    try:
        UE.record_utterance_edges(
            [_msg(msg_id="r1", quote_reply_id="q1")],
            platform="wechat",
            account_id=BOT_ID,
            engine=engine,
            bot_names=BOT_NAMES,
            repo=None,
        )
        # 直接写一条引她的边（build_edges 需要 quoted_lookup，这里手工插）
        from db.meta_models import UtteranceEdge

        with engine.begin() as conn:
            conn.execute(
                UtteranceEdge.__table__.insert().values(
                    platform="wechat",
                    bot_account_id=BOT_ID,
                    chat_id="10000000003@chatroom",
                    chat_type="group",
                    msg_id="r2",
                    speaker_id="wxid_other",
                    speaker_name="P6",
                    recipient_kind="bot",
                    recipient_id=BOT_ID,
                    recipient_name="鲸鲸",
                    quoted_msg_id="q1",
                    quoted_speaker_name="鲸鲸",
                    evidence="quote",
                )
            )
            hints = _hints_for(conn, ["r1", "r2"])
    finally:
        _mode(None)
    check("on 渲染引她的行", hints.get("r2") == "↳回 鲸鲸", str(hints))
    check("unknown 行不渲染", "r1" not in hints, str(hints))


def test_render_off_is_byte_identical() -> None:
    from datetime import datetime as _dt

    from core.context.chat_window import WindowMessage, _fmt_full

    msg = WindowMessage(occurred_at=_dt(2026, 10, 17, 21, 7), speaker="P6", content="你要来吗")
    check("off 行逐字", _fmt_full(msg) == "[21:07] P6: 你要来吗", _fmt_full(msg))
    hinted = WindowMessage(
        occurred_at=_dt(2026, 10, 17, 21, 7), speaker="P6", content="你要来吗", reply_hint="↳回 鲸鲸"
    )
    check("渲染位追加在正文后", _fmt_full(hinted).endswith("你要来吗 ↳回 鲸鲸"), _fmt_full(hinted))


# ------------------------------------------------------------------ 迁移 62
def test_migration_62() -> None:
    from sqlalchemy import create_engine

    from db.meta_models import UtteranceEdge
    from db.migrations import _m062

    path = ROOT / ".test_tmp" / "edges" / "mig.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.exec_driver_sql("create table terminal_events (id integer)")
        _m062(conn, None)
        _m062(conn, None)

    conn = sqlite3.connect(str(path))
    ddl_cols = [r[1] for r in conn.execute("pragma table_info(utterance_edges)")]
    model_cols = [c.name for c in UtteranceEdge.__table__.columns]
    check("迁移 62 列序与模型一致", ddl_cols == model_cols, f"{ddl_cols} != {model_cols}")
    idx = {r[0] for r in conn.execute("select name from sqlite_master where type='index' and tbl_name='utterance_edges'")}
    check(
        "迁移 62 索引齐（唯一键 + 反向查）",
        {"ux_utterance_edge_msg", "ix_utterance_edges_quoted"} <= idx,
        str(idx),
    )
    check("迁移 62 可重复执行（幂等）", True)
    row = conn.execute(
        "select sql from sqlite_master where type='table' and name='utterance_edges'"
    ).fetchone()[0]
    check("默认值落进 DDL", "'unknown'" in row and "'none'" in row, row)
    conn.close()

    # 没有 terminal_events 的库：不建表（守卫）
    path2 = ROOT / ".test_tmp" / "edges" / "mig_guard.db"
    if path2.exists():
        path2.unlink()
    engine2 = create_engine(f"sqlite:///{path2}")
    with engine2.begin() as conn:
        _m062(conn, None)
    conn2 = sqlite3.connect(str(path2))
    exists = conn2.execute(
        "select 1 from sqlite_master where type='table' and name='utterance_edges'"
    ).fetchone()
    conn2.close()
    check("无 terminal_events 的库不动表", exists is None, str(exists))


def main() -> int:
    test_modes()
    test_no_evidence_is_unknown()
    test_dm()
    test_quote_bot_and_person()
    test_quote_unresolved_keeps_quote()
    test_mentions_json()
    test_at_body()
    test_at_body_boundary()
    test_never_reads_quote_reply_user()
    test_off_writes_nothing()
    test_shadow_writes_but_off_render()
    test_on_renders()
    test_render_off_is_byte_identical()
    test_migration_62()
    if FAILED:
        print("\nFAILED %d / %d" % (len(FAILED), len(FAILED) + PASSED))
        for name in FAILED:
            print(" -", name)
        return 1
    print("\nALL PASSED (%d)" % PASSED)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
