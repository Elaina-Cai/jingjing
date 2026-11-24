# -*- coding: utf-8 -*-
r"""task-47 影子读数：既有错 @ 在「名单合法性 / 合理性」下会怎么判。

只读、可复算：拿日志里既有的 ``addressee_repaired``（模型自己写的 @ 目标被
出口判成「不在允许集合」的那些行），用``core.context.group_members.judge_address``
按**当前生产库的群名单 + 近期说话人**重放一遍，回答三件事：

1. 这些 @ 里有多少「名字能在本群名单里解析到」（之前只能靠人工猜）；
2. 判成 ``legal`` / ``unlikely`` / 非法（``unresolved`` / ``not_member`` /
   ``ambiguous``）各多少 —— **非法才是 on 档会剥离的**；
3. 抽 20 条人工清单（含上下文：她那句、@ 了谁、触发者、进群时间外的其他名字）。

复算：
    .\.venv_it\Scripts\python.exe -X utf8 -u scripts\report_addressee_roster.py
    .\.venv_it\Scripts\python.exe -X utf8 -u scripts\report_addressee_roster.py --sample 40
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import glob
import io
import json
import pathlib
import sqlite3
import sys
from collections import Counter, defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import temporal  # noqa: E402
from core.context.group_members import (  # noqa: E402
    ROSTER_SOURCE_MEMBERSHIP,
    active_hours as G_ACTIVE_HOURS,
    judge_address,
)

JOIN_WINDOW_SECONDS = 90.0


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, adapter):
        self._adapter = adapter

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, stmt, params=None):
        return _Rows(self._adapter.query(str(stmt), dict(params or {})))


class _Engine:
    def __init__(self, adapter):
        self._adapter = adapter

    def connect(self):
        return _Conn(self._adapter)


class _Person:
    """``find_persons`` 返回值的替身（判定只读 ``person_id``）。"""

    def __init__(self, person_id: object):
        self.person_id = str(person_id or "")


class ReadOnlyRepo:
    """把只读 sqlite 句柄伪装成 ``meta_repo``（只实现判定用到的两个读口）。"""

    class _People:
        """只读版 ``PeopleRepository.find_persons``：display_name / identity VALUE 都
        用 ``contains``（LIKE %q%）—— 与生产同为子串匹配，否则 ``应星`` 找不到
        ``应星\ue335`` 这类带私有区字符的群名片，重放会凭空报 ``unresolved``。"""

        def __init__(self, conn: "sqlite3.Connection"):
            self._conn = conn

        def find_persons(self, query: str, *, platform: str = "",
                         account_id: str = "", limit: int = 20):
            value = str(query or "").strip()
            if not value:
                return []
            like = f"%{value}%"
            sql = (
                "select distinct pe.person_id from person_entities pe "
                "left join person_identities pi on pi.person_id = pe.person_id "
                "where pe.status = 'active' "
                "and (pe.display_name like ? or pi.identity_value like ?)"
            )
            params: list[object] = [like, like]
            if platform:
                sql += " and pe.platform = ?"
                params.append(platform)
            sql += " limit ?"
            params.append(int(limit))
            rows = self._conn.execute(sql, params).fetchall()
            return [_Person(r[0]) for r in rows]

    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.engine = _Engine(self)
        self.people = self._People(self.conn)
        self._members: dict[str, list[tuple[str, str]]] = {}
        self._recent: dict[tuple[str, int], list[tuple[str]]] = {}

    def query(self, stmt: str, params: dict) -> list[tuple]:
        chat = str(params.get("chat") or "")
        if "group_memberships" in stmt:
            if chat not in self._members:
                # 必须带出 wxid：terminal_events.actor_person_id 放的是账号 id，
                # 只比 meta 人 id 会 100% 对不上（task-47 实测 0/166）。
                rows = self.conn.execute(
                    "select gm.group_nickname, gm.person_id, pi.identity_value "
                    "from group_memberships gm "
                    "left join person_identities pi "
                    "  on pi.person_id = gm.person_id "
                    " and pi.platform = gm.platform "
                    " and pi.identity_type = 'wxid' "
                    "where gm.chat_id = ? and gm.group_nickname != ''", (chat,),
                ).fetchall()
                self._members[chat] = [
                    (str(n or ""), str(p or ""), str(w or "")) for n, p, w in rows
                ]
            return self._members[chat]
        if "terminal_events" in stmt:
            since = str(params.get("since") or "")
            limit = int(params.get("lim") or 0)
            key = (chat, since, limit)
            if key not in self._recent:
                if since:
                    rows = self.conn.execute(
                        "select distinct actor_person_id from terminal_events "
                        "where chat_id = ? and actor_person_id != '' "
                        "and occurred_at >= ?", (chat, since),
                    ).fetchall()
                else:
                    rows = self.conn.execute(
                        "select actor_person_id from terminal_events "
                        "where chat_id = ? and actor_person_id != '' "
                        "order by occurred_at desc limit ?", (chat, limit),
                    ).fetchall()
                self._recent[key] = [(str(r[0] or ""),) for r in rows]
            return self._recent[key]
        return []


def load_repaired() -> list[dict]:
    rows: list[dict] = []
    for path in sorted(glob.glob(str(ROOT / "logs" / "yinyue.log*"))):
        try:
            handle = io.open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"addressee_repaired"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("event") == "addressee_repaired":
                    rows.append(rec)
    rows.sort(key=lambda item: str(item.get("timestamp") or ""))
    return rows


def load_shadow(event: str = "addressee_guard_shadow") -> list[dict]:
    """影子记录（R0 删门后 `addressee_guard_shadow` 不再产生，读数恒空）。"""
    rows: list[dict] = []
    for path in sorted(glob.glob(str(ROOT / "logs" / "yinyue.log*"))):
        try:
            handle = io.open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if f'"{event}"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("event") == event:
                    rows.append(rec)
    rows.sort(key=lambda item: str(item.get("timestamp") or ""))
    return rows


def _pid_probe(repo: "ReadOnlyRepo"):
    """把「名字 → 人 id」包成 canonical ``person_id_of`` 的形状（唯一才返回）。

    ``judge_address`` 在生产里会拿到 ``AddresseeResolver.person_id_of``；回放不注入它
    时，只靠本群名单/近期说话人/inbound 解析，会把「全局名字表能解析、但群名片不同」
    的真实成员误判成 unresolved（实测 1/39）。
    """
    from core.context.group_members import _person_ids_of_name

    def probe(name: str, *, platform: str = "") -> str:
        ids = _person_ids_of_name(str(name or "").strip(), platform=platform, meta_repo=repo)
        return next(iter(ids)) if len(ids) == 1 else ""

    return probe


def shadow_diff(repo: "ReadOnlyRepo", recent_limit: int) -> int:
    """把影子记录按**当前代码**重算一遍，给 before/after 对照（修歧义收窄用）。"""
    shadow = load_shadow()
    if not shadow:
        print("没有 addressee_guard_shadow 行（该事件随 R0 删门已停，读数恒空）")
        return 0
    before_amb = sum(1 for rec in shadow if rec.get("ambiguous"))
    print(f"影子记录：{len(shadow)} 条（其中 ambiguous={before_amb}）")
    transitions: Counter = Counter()
    after_amb = 0
    regressions: list[str] = []
    unreconstructable: list[str] = []
    resolved_names: Counter = Counter()
    # 还原生产那一次的调用上下文：trigger/allowed 从同 sha12 的 `addressee_repaired`
    # 取回，inbound 用「同群前 90s 内最近一条入站」——否则会凭空造出回归。
    repaired_by_sha = {
        str(rec.get("raw_text_sha12") or ""): rec for rec in load_repaired()
    }
    stamps = [m for m in (_local_stamp(rec) for rec in shadow) if m]
    chats = {str(rec.get("chat_id") or "") for rec in shadow}
    inbound = load_inbound(chats, min(stamps), max(stamps), repo) if stamps else {}
    for rec in shadow:
        chat = str(rec.get("chat_id") or "")
        text = str(rec.get("raw_text") or "")
        if "@" not in text:
            text = "@" + str(rec.get("at_name") or "")
        twin = repaired_by_sha.get(str(rec.get("raw_text_sha12") or ""), {})
        moment = _local_stamp(rec)
        joined = nearest_inbound(inbound.get(chat, ([], [])), moment) if moment else None
        verdict = judge_address(
            text,
            chat_id=chat,
            platform=str(rec.get("platform") or ""),
            trigger_name=str(twin.get("trigger") or ""),
            inbound=joined[1][1] if joined else "",
            recent_limit=recent_limit,
            now=moment,
            meta_repo=repo,
            person_id_of=_pid_probe(repo),
            known_names=tuple(twin.get("allowed") or ()),
        )
        after_reason = "no_address" if verdict is None else verdict.reason
        after_legal = bool(verdict is not None and verdict.legal)
        if verdict is not None and verdict.ambiguous:
            after_amb += 1
            resolved_names[str(verdict.name)] += 1
        transitions[(str(rec.get("reason") or "?"), after_reason)] += 1
        if rec.get("legal") and not after_legal:
            line = (
                f"{rec.get('timestamp')} {chat} @{rec.get('at_name')} "
                f"{rec.get('reason')} -> {after_reason}"
            )
            if not twin and str(rec.get("person_id") or "").strip():
                # 老路「本来就合法」的回合不会写 `addressee_repaired`，回放因此拿不到
                # 触发者/@ 的允许集合；这类行不是回归，是**回放无法重建上下文**。
                unreconstructable.append(line)
            else:
                regressions.append(line)
    print("\n[before → after 判定变化]")
    for (old, new), count in transitions.most_common():
        mark = "" if old == new else "   <-- 变了"
        print(f"  {old:<30} -> {new:<30} {count:>4}{mark}")
    print(f"\nambiguous：before {before_amb} → after {after_amb}")
    if resolved_names:
        print("  after 仍 ambiguous 的名字："
              + ", ".join(f"{name}x{count}" for name, count in resolved_names.most_common()))
    print(f"原本 allowed 现在非法（回归）：{len(regressions)}")
    if unreconstructable:
        print(f"（另有 {len(unreconstructable)} 条是回放无法重建上下文，不计入回归）")
        for item in unreconstructable[:3]:
            print("  ·", item)
    for line in regressions[:10]:
        print("  " + line)
    return 0


def _local_stamp(record: dict) -> dt.datetime | None:
    raw = str(record.get("timestamp") or "").replace("Z", "+00:00")
    try:
        moment = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return temporal.to_naive_local(moment)


def load_inbound(chats: set[str], start: dt.datetime, end: dt.datetime,
                 repo: ReadOnlyRepo) -> dict[str, tuple[list[dt.datetime], list[tuple]]]:
    window_start = (start - dt.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    window_end = (end + dt.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    out: dict[str, tuple[list[dt.datetime], list[tuple]]] = {}
    for chat in chats:
        rows = repo.conn.execute(
            "select occurred_at, actor_person_id, content from terminal_events "
            "where chat_id = ? and occurred_at >= ? and occurred_at <= ? "
            "order by occurred_at", (chat, window_start, window_end),
        ).fetchall()
        stamps: list[dt.datetime] = []
        payload: list[tuple] = []
        for occurred_at, actor, content in rows:
            try:
                moment = dt.datetime.fromisoformat(str(occurred_at))
            except ValueError:
                continue
            stamps.append(moment)
            payload.append((str(actor or ""), str(content or "")))
        out[chat] = (stamps, payload)
    return out


def speakers_within(conn, chat: str, moment: dt.datetime, hours: float) -> set[str]:
    """该群在 ``moment`` 之前 ``hours`` 小时内说过话的人（人 id 集合）。"""
    start = (moment - dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    end = moment.strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "select distinct actor_person_id from terminal_events "
        "where chat_id = ? and occurred_at >= ? and occurred_at <= ? "
        "and actor_person_id != ''", (chat, start, end),
    ).fetchall()
    return {str(row[0] or "") for row in rows}


def _ids_of(repo: "ReadOnlyRepo", chat: str, person_id: str) -> set[str]:
    """这个人的全部身份 id（meta 人 id + wxid）。"""
    from core.context.group_members import roster_snapshot

    return set(roster_snapshot(chat, meta_repo=repo).ids_of(person_id)) | {person_id}


def roster_ids(repo: "ReadOnlyRepo", chat: str) -> set[str]:
    from core.context.group_members import roster_snapshot

    return set(roster_snapshot(chat, meta_repo=repo).all_ids)


def nearest_inbound(index, moment: dt.datetime) -> tuple[dt.datetime, tuple] | None:
    stamps, payload = index
    if not stamps or moment is None:
        return None
    pos = bisect.bisect_right(stamps, moment)
    if pos == 0:
        return None
    stamp = stamps[pos - 1]
    if (moment - stamp).total_seconds() > JOIN_WINDOW_SECONDS:
        return None
    return stamp, payload[pos - 1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meta-db", default=str(ROOT / "data" / "yinyue_meta.db"))
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--recent-limit", type=int, default=20)
    parser.add_argument("--json", default="")
    parser.add_argument("--shadow-diff", action="store_true",
                        help="把生产影子记录按当前代码重算，出 before/after 对照")
    args = parser.parse_args()
    if args.shadow_diff:
        return shadow_diff(ReadOnlyRepo(args.meta_db), args.recent_limit)

    repaired = load_repaired()
    if not repaired:
        print("没有 addressee_repaired 行（日志里没出现过错 @）")
        return 0
    stamps = [moment for moment in (_local_stamp(rec) for rec in repaired) if moment]
    if not stamps:
        print("日志时间戳都解析失败")
        return 1
    repo = ReadOnlyRepo(args.meta_db)
    chats = {str(rec.get("chat_id") or "") for rec in repaired}
    inbound = load_inbound(chats, min(stamps), max(stamps), repo)

    verdicts: list[dict] = []
    for rec in repaired:
        chat = str(rec.get("chat_id") or "")
        moment = _local_stamp(rec)
        joined = nearest_inbound(inbound.get(chat, ([], [])), moment) if moment else None
        inbound_text = joined[1][1] if joined else ""
        verdict = judge_address(
            str(rec.get("raw_text") or ""),
            chat_id=chat,
            platform=str(rec.get("provider") or ""),
            trigger_name=str(rec.get("trigger") or ""),
            inbound=inbound_text,
            recent_limit=args.recent_limit,
            now=moment,
            meta_repo=repo,
        )
        verdicts.append({
            "record": rec,
            "moment": moment.strftime("%m-%d %H:%M") if moment else "",
            "inbound": inbound_text[:80],
            "verdict": verdict,
        })

    reasons = Counter()
    outcomes = Counter()
    per_chat: dict[str, Counter] = defaultdict(Counter)
    for item in verdicts:
        verdict = item["verdict"]
        chat = str(item["record"].get("chat_id") or "")
        if verdict is None:
            reasons["no_address"] += 1
            outcomes["no_address"] += 1
            per_chat[chat]["no_address"] += 1
            continue
        reasons[verdict.reason] += 1
        if verdict.legal:
            outcomes["legal_unlikely" if verdict.unlikely else "legal"] += 1
        else:
            outcomes["illegal"] += 1
        per_chat[chat][verdict.reason] += 1

    total = len(verdicts)
    print(f"addressee_repaired 行：{total}（日志 {len(glob.glob(str(ROOT / 'logs' / 'yinyue.log*')))} 个文件）")
    print(f"窗口：{stamps_local(min(stamps))} → {stamps_local(max(stamps))}")
    print("\n[reason]")
    for key, count in reasons.most_common():
        print(f"  {key:>12}  {count:>4}  {count / total:6.1%}")
    print("\n[on 档影响面]")
    for key in ("legal", "legal_unlikely", "illegal", "no_address"):
        count = outcomes.get(key, 0)
        print(f"  {key:>14}  {count:>4}  {count / total:6.1%}")
    print(f"\n  非法（on 会剥离）：{outcomes.get('illegal', 0)} / {total}")
    print(f"  合法但 unlikely（只记录）：{outcomes.get('legal_unlikely', 0)} / {total}")
    print("\n[按群]")
    for chat, counter in sorted(per_chat.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"  {chat:<28} " + " ".join(f"{k}={v}" for k, v in counter.most_common()))

    legal_items = [i for i in verdicts if i["verdict"] is not None and i["verdict"].legal]
    legal_total = len(legal_items)
    print("\n[合理性判据对照：同一批合法 @ 目标「算不算合理」]")
    print(f"  legal 目标：{legal_total} 条（判据只影响 unlikely，不影响合法性）")
    print("  判据                        plausible   unlikely")
    for hours in (24, 72, 168):
        plausible = 0
        for item in legal_items:
            chat = str(item["record"].get("chat_id") or "")
            moment = _local_stamp(item["record"])
            speakers = speakers_within(repo.conn, chat, moment, float(hours)) if moment else set()
            if _ids_of(repo, chat, item["verdict"].person_id) & speakers:
                plausible += 1
        print(f"  近 {hours}h 在群说过话{' ' * max(0, 12 - len(str(hours)))}"
              f"{plausible:>5}/{legal_total:<5} {legal_total - plausible:>5}"
              f"  ({(legal_total - plausible) / max(legal_total, 1):6.1%})")
    window_hit = sum(1 for item in legal_items if item["verdict"].plausible)
    print(f"  {'卡面近 20 条窗口':<22}{window_hit:>5}/{legal_total:<5} "
          f"{legal_total - window_hit:>5}  ({(legal_total - window_hit) / max(legal_total, 1):6.1%})")
    print(f"  判据口径：YINYUE_ADDRESSEE_ROSTER_ACTIVE_HOURS={G_ACTIVE_HOURS()}")

    samples = _pick_samples(verdicts, args.sample)
    print(f"\n[人工清单 {len(samples)} 条]")
    for item in samples:
        verdict = item["verdict"]
        rec = item["record"]
        label = "无@" if verdict is None else (
            f"{verdict.reason}/{'legal' if verdict.legal else 'illegal'}"
            f"{'/unlikely' if verdict.unlikely else ''}")
        print(f"  - {item['moment']} {rec.get('chat_id')} [{label}] "
              f"@ {verdict.name if verdict else '-'} → person={verdict.person_id if verdict else '-'} "
              f"roster={verdict.in_roster if verdict else '-'} recent={verdict.recent if verdict else '-'}")
        print(f"    她那句：{str(rec.get('raw_text') or '')[:60]}")
        print(f"    触发者：{rec.get('trigger')}  允许集合：{rec.get('allowed')}")
        print(f"    上文  ：{item['inbound']}")

    if args.json:
        payload = [
            {
                "moment": item["moment"],
                "chat_id": item["record"].get("chat_id"),
                "raw_text": item["record"].get("raw_text"),
                "trigger": item["record"].get("trigger"),
                "inbound": item["inbound"],
                "verdict": item["verdict"].as_dict() if item["verdict"] else None,
            }
            for item in verdicts
        ]
        pathlib.Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\njson → {args.json}")
    return 0


def stamps_local(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


def _pick_samples(verdicts: list[dict], limit: int) -> list[dict]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for item in verdicts:
        verdict = item["verdict"]
        if verdict is None:
            buckets["no_address"].append(item)
        elif verdict.legal and verdict.unlikely:
            buckets["legal_unlikely"].append(item)
        elif verdict.legal:
            buckets["legal"].append(item)
        elif verdict.ambiguous:
            buckets["ambiguous"].append(item)
        else:
            buckets[verdict.reason].append(item)
    order = ["ambiguous", "not_member", "unresolved", "legal_unlikely", "legal", "no_address"]
    out: list[dict] = []
    index = 0
    while len(out) < limit:
        added = False
        for key in order:
            bucket = buckets.get(key) or []
            if index < len(bucket):
                out.append(bucket[index])
                added = True
                if len(out) >= limit:
                    break
        if not added:
            break
        index += 1
    return out


if __name__ == "__main__":
    raise SystemExit(main())
