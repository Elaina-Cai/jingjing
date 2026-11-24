r"""task-42：「谁对谁说」做成事件一等边的只读可行性量具。

量三组数（全部只读；生产库一律 ``file:...?mode=ro`` 打开，成本段只动
``.test_tmp/edge/`` 下的副本）：

1. ``availability`` —— 近 N 天账号库 ``messages`` 里 ``quote_reply_id`` /
   ``mentions`` 的非空率（分母 = 入站 ``is_self=0``），以及**内容可用性**：
   ``quote_reply_user`` 能不能直接当人名、``quote_reply_id`` 能不能在
   ``messages.msg_id`` 里解析回原话与发言人。
2. ``join`` —— 把这些边按 ``platform/chat_id/external_msg_id`` join 到
   ``terminal_events``，能覆盖多少条事件；补出的边里 ``recipient_kind`` 分布。
3. ``cost`` —— 「terminal_events 加列」vs「单独 utterance_edges 表」的迁移/
   回填耗时与体积，以及窗口多渲染一行的 prompt 增量（字符中位数）。

``recipient_kind`` 口径（写死在 ``_kind_of``）：

* ``bot``     —— 被引用的那条 ``messages.is_self=1``（引用的是鲸鲸自己）
* ``person``  —— 被引用的那条解析到了具体发言人（``sender_name`` 非空）
* ``group``   —— 引用了、但解析不出人名（只能算"对着群里说"）
* ``unknown`` —— ``quote_reply_id`` 在 ``messages.msg_id`` 里找不到 ⇒ 不猜

命令::

    & .\.venv_it\Scripts\python.exe -X utf8 scripts\probe_utterance_edges.py
    & .\.venv_it\Scripts\python.exe -X utf8 scripts\probe_utterance_edges.py --sections availability,join
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import statistics
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
META_DB = ROOT / "data/yinyue_meta.db"
WECHAT_DB = ROOT / "data/platforms/wechat/wxid_seed02/account.db"
QQ_DB = ROOT / "data/platforms/qq/3580199493/account.db"
COPY_DIR = ROOT / ".test_tmp/edge"

BOT_ACCOUNT = "wxid_seed02"
MAIN_CHAT = "10000000003@chatroom"
SECOND_CHAT = "10000000001@chatroom"
QQ_CHAT = "group:1124196924"

SECTIONS = ("availability", "join", "cost")


def _ro(path: Path) -> sqlite3.Connection:
    """只读打开（uri=True + mode=ro）：不碰生产可写句柄。"""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _window_start(con: sqlite3.Connection, days: float) -> str:
    end = con.execute("select max(created_at) from messages").fetchone()[0]
    end_dt = dt.datetime.fromisoformat(str(end))
    return (end_dt - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _kind_of(quoted: tuple | None) -> str:
    if quoted is None:
        return "unknown"
    _mid, is_self, sender_name, _content = quoted
    if is_self == 1:
        return "bot"
    return "person" if (sender_name or "").strip() else "group"


def _resolve(con: sqlite3.Connection, ids: list[str]) -> dict[str, tuple]:
    """把 msg_id 批量解析成 (msg_id, is_self, sender_name, content)。"""
    out: dict[str, tuple] = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i : i + 400]
        sql = (
            "select cast(msg_id as text), is_self, sender_name, content from messages "
            "where cast(msg_id as text) in (%s)" % ",".join("?" * len(chunk))
        )
        for r in con.execute(sql, chunk):
            out[r[0]] = r
    return out


def _quote_rows(con: sqlite3.Connection, chat: str, start: str) -> list[tuple]:
    return con.execute(
        "select cast(msg_id as text), quote_reply_id from messages "
        "where chat_id=? and is_self=0 and quote_reply_id is not null and quote_reply_id!='' "
        "and created_at>=?",
        (chat, start),
    ).fetchall()


def section_availability(days: float) -> None:
    con = _ro(WECHAT_DB)
    start = _window_start(con, days)
    print(f"\n## 1. 可得率（wechat 账号库 {WECHAT_DB.name}，入站 is_self=0，窗口 >= {start}）")
    print(f"   {'chat_id':28s} {'type':8s} {'inbound':>7s} {'quote':>6s} {'rate':>7s} {'mentions':>8s}")
    chats = [
        r[0]
        for r in con.execute(
            "select chat_id from messages where is_self=0 and created_at>=? "
            "group by chat_id order by count(*) desc limit 6",
            (start,),
        )
    ]
    for chat in chats:
        row = con.execute(
            "select chat_type, count(*), "
            "sum(case when quote_reply_id is not null and quote_reply_id!='' then 1 else 0 end), "
            "sum(case when mentions is not null and mentions!='' then 1 else 0 end) "
            "from messages where chat_id=? and is_self=0 and created_at>=?",
            (chat, start),
        ).fetchone()
        ctype, inbound, quote_n, ment_n = row
        rate = f"{quote_n / max(inbound, 1) * 100:.2f}%"
        print(f"   {chat:28s} {str(ctype):8s} {inbound:7d} {quote_n:6d} {rate:>7s} {ment_n:8d}")

    print("\n   内容可用性（主群 quote_reply_user 能不能当人名）：")
    for val, n in con.execute(
        "select quote_reply_user, count(*) c from messages where chat_id=? "
        "and quote_reply_id is not null and quote_reply_id!='' and created_at>=? "
        "group by 1 order by c desc limit 5",
        (MAIN_CHAT, start),
    ):
        flag = "= chat_id（不可用）" if val == MAIN_CHAT else ""
        print(f"     {str(val)[:34]:36s} {n:4d} {flag}")

    print("\n   quote_reply_id -> messages.msg_id 解析率（主群）：")
    rows = _quote_rows(con, MAIN_CHAT, start)
    ids = sorted({r[1] for r in rows})
    found = _resolve(con, ids)
    resolved_rows = sum(1 for r in rows if r[1] in found)
    named = sum(1 for v in found.values() if (v[2] or "").strip())
    print(f"     引用的入站消息 {len(rows)} 条 / 去重引用 id {len(ids)} 个 -> 解析到 {len(found)} 个 "
          f"({len(found) / max(len(ids), 1) * 100:.1f}%)，覆盖 {resolved_rows} 条消息 "
          f"({resolved_rows / max(len(rows), 1) * 100:.1f}%)；解析出的发言人有名 {named}/{len(found)}")

    print("\n   recipient_kind 分布（口径见文件头 _kind_of）：")
    kinds = {"bot": 0, "person": 0, "group": 0, "unknown": 0}
    for _mid, qid in rows:
        kinds[_kind_of(found.get(qid))] += 1
    total = max(len(rows), 1)
    print("     " + "  ".join(f"{k}={v} ({v / total * 100:.1f}%)" for k, v in kinds.items()))
    for chat in (SECOND_CHAT,):
        r2 = _quote_rows(con, chat, start)
        i2 = sorted({r[1] for r in r2})
        f2 = _resolve(con, i2)
        k2 = {"bot": 0, "person": 0, "group": 0, "unknown": 0}
        for _mid, qid in r2:
            k2[_kind_of(f2.get(qid))] += 1
        print(f"     副群 {chat}: {len(r2)} 条引用 -> {k2}")

    print("\n   QQ mentions（结构化，但 user_name 恒空，需 contacts 反查）：")
    q = _ro(QQ_DB)
    ids_q = [
        json.loads(r[0])[0]["user_id"]
        for r in q.execute(
            "select mentions from messages where mentions is not null and mentions!=''"
        )
    ]
    resolved = 0
    for uid in ids_q:
        hit = q.execute("select nickname from contacts where wxid=?", (uid,)).fetchone()
        resolved += 1 if hit and (hit[0] or "").strip() else 0
    print(f"     mentions 非空 {len(ids_q)} 条 -> contacts 反查到昵称 {resolved} 条 "
          f"({resolved / max(len(ids_q), 1) * 100:.0f}%)")
    q.close()
    con.close()


def section_join(days: float) -> None:
    con = _ro(WECHAT_DB)
    meta = _ro(META_DB)
    start = _window_start(con, days)
    total = meta.execute("select count(*) from terminal_events").fetchone()[0]
    with_ext = meta.execute(
        "select count(*) from terminal_events where external_msg_id is not null and external_msg_id!=''"
    ).fetchone()[0]
    print(f"\n## 2. 能补出多少边（terminal_events 窗口 >= {start}）")
    print(f"   terminal_events 总计 {total} 行，其中有 external_msg_id {with_ext} 行")
    print(f"   {'chat_id':28s} {'events':>7s} {'with_ext':>9s} {'self':>6s}")
    for chat, n, self_n, wext in meta.execute(
        "select chat_id, count(*), sum(case when actor_account_id=bot_account_id then 1 else 0 end), "
        "sum(case when external_msg_id is not null and external_msg_id!='' then 1 else 0 end) "
        "from terminal_events where platform='wechat' and occurred_at>=? "
        "group by chat_id order by count(*) desc limit 6",
        (start,),
    ):
        print(f"   {chat:28s} {n:7d} {wext:9d} {self_n:6d}")

    ext_ids = [
        r[0]
        for r in meta.execute(
            "select external_msg_id from terminal_events where platform='wechat' "
            "and chat_id=? and external_msg_id is not null and external_msg_id!='' and occurred_at>=?",
            (MAIN_CHAT, start),
        )
    ]
    hit = 0
    for i in range(0, len(ext_ids), 400):
        chunk = ext_ids[i : i + 400]
        sql = (
            "select count(*) from messages where chat_id=? and cast(msg_id as text) in (%s)"
            % ",".join("?" * len(chunk))
        )
        hit += con.execute(sql, [MAIN_CHAT, *chunk]).fetchone()[0]
    print(f"\n   主群事件 -> messages 对得上：{hit}/{len(ext_ids)} ({hit / max(len(ext_ids), 1) * 100:.1f}%)")

    rows = _quote_rows(con, MAIN_CHAT, start)
    ext = set(ext_ids)
    in_events = sum(1 for r in rows if r[0] in ext)
    pct = in_events / max(len(rows), 1) * 100
    print(f"   主群带引用的入站 {len(rows)} 条，其中在 terminal_events 里有对应行 {in_events} "
          f"({pct:.1f}%) ⇒ 这些就是可回填的边")
    con.close()
    meta.close()


def section_cost(days: float, slow: bool) -> None:
    COPY_DIR.mkdir(parents=True, exist_ok=True)
    dst = COPY_DIR / "meta_cost_copy.db"
    for f in (dst, Path(str(dst) + "-wal"), Path(str(dst) + "-shm")):
        if f.exists():
            f.unlink()
    t0 = time.perf_counter()
    shutil.copy2(META_DB, dst)
    print(f"\n## 3. 代价（副本 {dst.relative_to(ROOT)}，{os.path.getsize(dst) / 1048576:.1f} MB，"
          f"复制 {time.perf_counter() - t0:.2f}s）")

    con = _ro(WECHAT_DB)
    start = _window_start(con, days)
    c = sqlite3.connect(dst)
    cc = c.cursor()
    t0 = time.perf_counter()
    cc.execute("alter table terminal_events add column addressee_kind text")
    c.commit()
    print(f"   方案A 迁移 DDL：alter table add column addressee_kind -> {time.perf_counter() - t0:.3f}s（只改元数据）")

    rows = _quote_rows(con, MAIN_CHAT, start)
    ids = sorted({r[1] for r in rows})
    found = _resolve(con, ids)
    edges = [(r[0], _kind_of(found.get(r[1]))) for r in rows]
    cc.execute("create temp table edges(ext text primary key, kind text)")
    cc.executemany("insert into edges values(?,?)", edges)
    t0 = time.perf_counter()
    cc.execute(
        "update terminal_events set addressee_kind="
        "(select e.kind from edges e where e.ext=terminal_events.external_msg_id) "
        "where external_msg_id in (select ext from edges)"
    )
    c.commit()
    print(f"   方案A 回填（集合式 UPDATE，{len(edges)} 条边）：{time.perf_counter() - t0:.2f}s")
    if slow:
        cc.execute("update terminal_events set addressee_kind=null")
        c.commit()
        sample = edges[:200]
        t0 = time.perf_counter()
        cc.executemany(
            "update terminal_events set addressee_kind=? where platform='wechat' "
            "and bot_account_id=? and chat_id=? and external_msg_id=?",
            [(k, "wechat", BOT_ACCOUNT, MAIN_CHAT, e) for e, k in sample],
        )
        c.commit()
        per = (time.perf_counter() - t0) / max(len(sample), 1)
        print(f"   方案A 回填（逐条 UPDATE，{len(sample)} 条取样）：{per * 1000:.1f} ms/条 "
              f"=> {len(edges)} 条约 {per * len(edges):.1f}s（比集合式慢两个数量级，别用）")

    t0 = time.perf_counter()
    cc.execute(
        "create table if not exists utterance_edges("
        "id integer primary key, platform text, chat_id text, external_msg_id text, "
        "reply_to_msg_id text, reply_to_actor text, reply_to_kind text, created_at text)"
    )
    cc.executemany(
        "insert into utterance_edges(platform,chat_id,external_msg_id,reply_to_kind) "
        "values('wechat',?,?,?)",
        [(MAIN_CHAT, e, k) for e, k in edges],
    )
    c.commit()
    print(f"   方案B 建表+回填（{len(edges)} 行）：{time.perf_counter() - t0:.2f}s；"
          f"副本体积 {os.path.getsize(dst) / 1048576:.2f} MB")

    print("\n   窗口渲染增量（一行 = '↩回<人名>「<原话前 20 字>」'）：")
    lines = [
        len(f"↩回{found[r[1]][2] or '某人'}「{(found[r[1]][3] or '')[:20]}」")
        for r in rows
        if r[1] in found
    ]
    if lines:
        lines.sort()
        print(f"     字符数 median={statistics.median(lines):.0f} mean={statistics.mean(lines):.1f} "
              f"p90={lines[int(len(lines) * 0.9)]} n={len(lines)}")
    inbound = con.execute(
        "select count(*) from messages where chat_id=? and is_self=0 and created_at>=?",
        (MAIN_CHAT, start),
    ).fetchone()[0]
    rate = len(lines) / max(inbound, 1)
    print(f"     带引用比例 {rate * 100:.1f}% ⇒ 20 条消息的窗口约多 {rate * 20:.1f} 行、"
          f"约 +{rate * 20 * statistics.median(lines):.0f} 字符")
    con.close()
    c.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="task-42 utterance edges 可行性量具（只读）")
    ap.add_argument("--sections", default=",".join(SECTIONS))
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--slow", action="store_true", help="额外量逐条 UPDATE 的慢路径")
    args = ap.parse_args()
    picked = [s.strip() for s in args.sections.split(",") if s.strip()]
    for s in picked:
        if s not in SECTIONS:
            raise SystemExit(f"unknown section: {s} (choose from {SECTIONS})")
    if "availability" in picked:
        section_availability(args.days)
    if "join" in picked:
        section_join(args.days)
    if "cost" in picked:
        section_cost(args.days, args.slow)


if __name__ == "__main__":
    main()
