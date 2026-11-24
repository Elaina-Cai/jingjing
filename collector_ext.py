"""2026-10-28 回滚后补回的两个只读聚合（小本本合并 / 送达率）。不写任何文件。"""
import json
import os
import time


def _root():
    return os.path.dirname(os.path.abspath(__file__))


def notebook2():
    """小本本 + 常驻板：优先读 loop.notes 的合并结果。"""
    days = []
    memo = []
    try:
        from loop import notes as _n
        for r in _n._all_rows():
            if str(r.get("kind")) == "diary":
                days.append({
                    "day": r.get("day") or str(r.get("at") or "")[:10],
                    "at": r.get("at") or "",
                    "chars": len(str(r.get("text") or "")),
                    "text": str(r.get("text") or ""),
                })
            else:
                memo.append({
                    "at": r.get("at") or "",
                    "kind": r.get("kind") or "",
                    "kind_label": (_n.KINDS or {}).get(r.get("kind") or "", ""),
                    "target": r.get("target") or "",
                    "text": str(r.get("text") or ""),
                })
    except Exception as exc:
        return {"days": [], "memo": [], "n_days": 0, "n_memo": 0, "error": repr(exc)[:140]}
    days.sort(key=lambda x: x["at"], reverse=True)
    memo.sort(key=lambda x: x["at"], reverse=True)
    return {
        "days": days[:40],
        "memo": memo[:60],
        "n_days": len(days),
        "n_memo": len(memo),
        "files": ["data/loop/notes.jsonl"],
        "src": "loop.notes",
    }


def say_stats2():
    """今天开口的送达率：只读当天的账本。"""
    day = time.strftime("%Y-%m-%d")
    path = os.path.join(_root(), "data", "loop", "ledger-%s.jsonl" % day)
    out = {"say": 0, "sent": 0, "blocked": 0, "failed": 0, "error": 0,
           "ack": {}, "reasons": [], "last_at": ""}
    reasons = {}
    if not os.path.isfile(path):
        return out
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("act") != "say":
                    continue
                out["say"] += 1
                res = str(r.get("result") or "")
                if res in ("sent", "blocked", "failed", "error"):
                    out[res] += 1
                ack = str(r.get("ack") or "")
                if ack:
                    out["ack"][ack] = out["ack"].get(ack, 0) + 1
                why = str(r.get("blocked_reason") or "")
                if why:
                    reasons[why] = reasons.get(why, 0) + 1
                out["last_at"] = str(r.get("at") or out["last_at"])
    except Exception as exc:
        out["error_note"] = repr(exc)[:140]
    out["reasons"] = [
        {"why": k, "n": v} for k, v in sorted(reasons.items(), key=lambda x: -x[1])[:5]
    ]
    return out


def ledger_state():
    """账本页：今天每次开口的明细（最近 60 条）+ 汇总。"""
    st = say_stats2()
    rows = []
    day = time.strftime("%Y-%m-%d")
    path = os.path.join(_root(), "data", "loop", "ledger-%s.jsonl" % day)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("act") != "say":
                        continue
                    rows.append({
                        "at": str(r.get("at") or ""),
                        "target": str(r.get("target") or ""),
                        "result": str(r.get("result") or ""),
                        "ack": str(r.get("ack") or ""),
                        "why": str(r.get("blocked_reason") or ""),
                        "text": str(r.get("text") or "")[:80],
                    })
        except Exception as exc:
            st["error_note"] = repr(exc)[:140]
    rows = rows[-60:]
    return {
        "n_say": st.get("say"),
        "sent": st.get("sent"),
        "blocked": st.get("blocked"),
        "failed": st.get("failed"),
        "error": st.get("error"),
        "ack": st.get("ack") or {},
        "reasons": st.get("reasons") or [],
        "rows": rows,
    }


def caps_text():
    """这台机器上哪些数据真能拿到（读 capabilities.json，不探测）。"""
    out = []
    base = os.path.join(_root(), "data", "platforms")
    if not os.path.isdir(base):
        return []
    for plat in os.listdir(base):
        pdir = os.path.join(base, plat)
        if not os.path.isdir(pdir):
            continue
        for acc in sorted(os.listdir(pdir)):
            cp = os.path.join(pdir, acc, "capabilities.json")
            if not os.path.isfile(cp):
                continue
            try:
                with open(cp, encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                continue
            caps = d.get("capabilities") or {}
            out.append({
                "platform": plat,
                "account": acc,
                "probed_at": d.get("probed_at") or "",
                "ok": [k for k, v in sorted(caps.items()) if v],
                "degraded": d.get("degraded") or [],
            })
    return out


def queue_state(limit: int = 8) -> dict:
    """面板用：投递队列实时概况。

    2026-10-28 教训：**必须自己开连接、自己关**。
    早前先走 world_db.queue_stats()，它复用进程级长连接，SELECT 后隐式事务不结束，
    面板读到的永远是第一次查询那一刻的快照（删掉的行还在、状态永远是 pending）。
    这里用一条短命连接 + autocommit，保证每次读都是最新。
    """
    import os as _os
    import sqlite3 as _sq
    out = {"ok": True, "n": 0, "pending": 0, "sending": 0, "sent": 0,
           "failed": 0, "expired": 0, "skipped": 0, "rows": [], "now": time.time()}
    db = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "data", "world.db")
    if not _os.path.exists(db):
        out["ok"] = False
        out["err"] = "data/world.db 不存在"
        return out
    try:
        con = _sq.connect(db, timeout=6.0, isolation_level=None)
        con.row_factory = _sq.Row
        try:
            for _st in ("pending", "sending", "sent", "failed", "expired", "skipped"):
                out[_st] = int(con.execute(
                    "select count(*) from outbound_queue where state=?", (_st,)).fetchone()[0])
                out["n"] += out[_st]
            rows = con.execute(
                "select id, state, chat_id, preview, attempts, ack, why, created_ts"
                " from outbound_queue where state in ('pending','sending','failed','expired')"
                " order by id desc limit ?", (int(limit),)).fetchall()
        finally:
            con.close()
    except Exception as _exc:
        out["ok"] = False
        out["err"] = repr(_exc)[:200]
        return out
    now = out["now"]
    for _r in rows:
        _born = float(_r["created_ts"] or now)
        out["rows"].append({
            "id": _r["id"], "state": _r["state"], "chat": _r["chat_id"] or "",
            "preview": str(_r["preview"] or "")[:40],
            "attempts": int(_r["attempts"] or 0), "ack": str(_r["ack"] or ""),
            "age_s": round(max(0.0, now - _born), 1), "why": str(_r["why"] or "")[:60],
        })
    return out

# ---------------------------------------------------------------------------
# 观察窗 · 送达对账（2026-11-01）
#
# 她这一拍说了什么 → 有没有进队列 → 队列里变成什么样 → 平台有没有回来认账。
# 她本人只看得到 `say` 返回的 "queued"，`sent/confirmed/failed` 不进她的观测；
# 这个函数把四段对起来，给面板和家人看。
# ---------------------------------------------------------------------------

_JOIN_WINDOW_S = 300.0
_DELIVERY_STATES = ("confirmed", "unconfirmed", "waiting", "failed", "expired",
                    "no_queue", "blocked", "error")


def _extra_ids(extra) -> list:
    """账本 extra 里的 queue_ids 可能 int / str 混着，统一成 int 列表。"""
    out = []
    for x in ((extra or {}).get("queue_ids") or []):
        try:
            out.append(int(x))
        except Exception:  # noqa: BLE001 - 一个脏值不该让整条开口找不到队列
            continue
    return out


def _ledger_says(hours: float) -> list:
    """窗口内账本里的每一次开口（跨天要读两个文件）。"""
    out = []
    now = time.time()
    cut = now - float(hours) * 3600.0
    day0 = time.strftime("%Y-%m-%d", time.localtime(cut - 86400))
    day1 = time.strftime("%Y-%m-%d", time.localtime(now + 86400))
    folder = os.path.join(_root(), "data", "loop")
    if not os.path.isdir(folder):
        return out
    for name in sorted(os.listdir(folder)):
        if not (name.startswith("ledger-") and name.endswith(".jsonl")):
            continue
        day = name[len("ledger-"):-len(".jsonl")]
        if not (day0 <= day <= day1):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    _act = str(r.get("act") or "")
                    # 2026-11-01：表情也走投递队列，对账里要算一次"开口"，
                    # 否则面板只看得见字、看不见图（她会发表情，但报表装没看见）。
                    if _act not in ("say", "sticker"):
                        continue
                    at = str(r.get("at") or "")
                    try:
                        ts = time.mktime(time.strptime(at[:19], "%Y-%m-%d %H:%M:%S"))
                    except Exception:
                        continue
                    if ts < cut:
                        continue
                    out.append({
                        "at": at, "ts": ts,
                        "target": str(r.get("target") or ""),
                        # 2026-11-01：一次开口拆成 N 个气泡，靠链接键才能和队列精确对上
                        "group_id": str((r.get("extra") or {}).get("group_id") or ""),
                        "queue_ids": _extra_ids(r.get("extra")),
                        "text": ("[表情] " + str(r.get("text") or "")) if _act == "sticker" else str(r.get("text") or ""),
                        "kind": "sticker" if _act == "sticker" else "text",
                        "why": str(r.get("why") or ""),
                        "result": str(r.get("result") or ""),
                        "blocked_reason": str(r.get("blocked_reason") or ""),
                        "turn_id": str(r.get("turn_id") or ""),
                    })
        except Exception as exc:
            out.append({"at": "", "ts": 0, "target": "", "text": "", "why": "",
                        "result": "ledger_unreadable", "blocked_reason": repr(exc)[:120],
                        "turn_id": ""})
    out.sort(key=lambda x: x["ts"])
    return out


def _queue_rows(hours: float) -> list:
    import sqlite3
    db = os.path.join(_root(), "data", "world.db")
    cut = time.time() - float(hours) * 3600.0
    rows = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/").replace("\\", "/"), uri=True, timeout=2.0)
    except Exception:
        try:
            con = sqlite3.connect(db, timeout=2.0)
        except Exception:
            return rows
    try:
        con.row_factory = sqlite3.Row
        # 2026-11-01：带上 group_id —— 一次开口在队列里是 N 个气泡（同一 group_id），
        # 没有这个键就只能拿文本前缀猜，一个气泡对一行，多气泡全对不上。
        sql = ("select id, chat_id, coalesce(group_id,'') as group_id,"
               " coalesce(preview,'') as preview, state,"
               " coalesce(ack,'') as ack, created_ts, settled_ts, coalesce(why,'') as why"
               " from outbound_queue where created_ts >= ? order by created_ts")
        for r in con.execute(sql, (cut,)):
            rows.append({k: r[k] for k in r.keys()})
    except Exception:
        pass
    finally:
        con.close()
    # 队列被清空/归档过的话，历史行不在库里了 —— 从 backups/queue-archive-*/ 补回来，
    # 否则"她说了、队列里没有"会被误判成黑洞（2026-11-01 实测 45 条里 26 条是这么来的）。
    seen = {r.get("id") for r in rows}
    arch = os.path.join(_root(), "backups")
    if os.path.isdir(arch):
        for name in sorted(os.listdir(arch)):
            if not name.startswith("queue-archive"):
                continue
            fip = os.path.join(arch, name, "outbound_queue.json")
            if not os.path.isfile(fip):
                continue
            try:
                with open(fip, encoding="utf-8") as f:
                    for r in json.load(f):
                        if not isinstance(r, dict) or r.get("id") in seen:
                            continue
                        if float(r.get("created_ts") or 0) < cut:
                            continue
                        rows.append({k: r.get(k) for k in (
                            "id", "chat_id", "group_id", "preview", "state", "ack",
                            "created_ts", "settled_ts", "why")})
                        seen.add(r.get("id"))
            except Exception:
                continue
    rows.sort(key=lambda x: float(x.get("created_ts") or 0))
    return rows


def _receipt_acks(hours: float) -> dict:
    """(chat_id, 文本前缀) → 平台认账状态。"""
    import sqlite3
    db = os.path.join(_root(), "data", "world.db")
    cut = time.time() - float(hours) * 3600.0
    out = {}
    try:
        con = sqlite3.connect(db, timeout=2.0)
    except Exception:
        return out
    try:
        con.row_factory = sqlite3.Row
        sql = ("select chat_id, coalesce(text,'') as text, coalesce(ack,'') as ack,"
               " coalesce(msg_id,'') as msg_id, coalesce(confirmed_at,'') as confirmed_at"
               " from outbound_receipt where sent_at_utc >= ?")
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(cut))
        for r in con.execute(sql, (since,)):
            key = (r["chat_id"], str(r["text"] or "")[:16])
            out[key] = {"ack": r["ack"], "msg_id": r["msg_id"], "confirmed_at": r["confirmed_at"]}
    except Exception:
        pass
    finally:
        con.close()
    return out


def _same_text(a: str, b: str) -> bool:
    a = "".join(str(a or "").split())[:14]
    b = "".join(str(b or "").split())[:14]
    return bool(a) and bool(b) and a == b


def _group_verdict(states: list, acks: list, rec_ack: str) -> str:
    """一组气泡 → 一个裁决。

    一次开口 = N 个气泡，缺一个都算没送到：任何气泡 failed/expired 整条就算失败；
    只有全部 sent 且全认账，这条开口才算 confirmed；还有气泡没发出去就是 waiting。
    """
    if "failed" in states:
        return "failed"
    if "expired" in states:
        return "expired"
    if states and all(s == "sent" for s in states):
        all_ack = all(a == "confirmed" for a in acks) or rec_ack == "confirmed"
        return "confirmed" if all_ack else "unconfirmed"
    if any(s in ("pending", "sending") for s in states):
        return "waiting"
    return "no_queue"


def _orphan_bubbles(rows: list, out_rows: list) -> list:
    """反向对账：队列里的气泡，哪个开口都没认领它。

    正常路径「开口 → 入队」一定在账本留一行；留不下的（自测、兜底、别的服务
    直接塞队列）就成了孤儿。它们是真发出去了的气泡，失败也只写在队列行上，
    从开口这一侧永远扫不到 —— 所以单独列出来给面板看。
    """
    claimed = set()
    for r in out_rows:
        for i in (r.get("queue_ids") or []):
            claimed.add(i)
    out = []
    for row in rows:
        if row.get("id") in claimed:
            continue
        try:
            at = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(float(row.get("created_ts") or 0)))
        except Exception:  # noqa: BLE001 - 时间戳坏了也要露出这一行
            at = ""
        out.append({
            "id": row.get("id"),
            "chat": str(row.get("chat_id") or ""),
            "group_id": str(row.get("group_id") or ""),
            "text": str(row.get("preview") or ""),
            "state": str(row.get("state") or ""),
            "ack": str(row.get("ack") or ""),
            "why": str(row.get("why") or ""),
            "at": at,
        })
    out.sort(key=lambda x: x["at"], reverse=True)
    return out[:20]


def audit_join(says: list, rows: list, receipts: dict) -> dict:
    """纯函数：把"她说的"和"队列里的"配起来（测试直接喂它）。

    2026-11-01：配对单位是「一条开口 ↔ 一组气泡」，不是一行对一行。
    一次开口在队列里留下 N 行，账本只记一句，所以要靠写入侧存下来的链接键
    把它们扣上。按可信度依次是：
      1. `queue_ids` —— 账本 extra 里的**队列行号**，一笔一个 id，最准；
      2. `group_id`  —— 整组共有的组号（`say_many` 整组入队那条路才有一个组号）；
      3. 时间窗 + 文本前 14 字 —— 老账本行的兜底，老数据不能因为改了键就塌成黑洞。
    有链接键却对不上的，如实算「没进队列」：不许退回去拿旁边的气泡顶上
    （跟"拿不到 referent 就换对象"是同一个毛病）。
    """
    by_group = {}
    by_id = {}
    for i, row in enumerate(rows):
        gid = str(row.get("group_id") or "")
        if gid:
            by_group.setdefault((str(row.get("chat_id") or ""), gid), []).append(i)
        try:
            by_id[int(row.get("id"))] = i
        except Exception:  # noqa: BLE001 - 行号不是整数（归档里可能缺）就只按组号找
            pass

    used = set()
    joins = {"queue_ids": 0, "group_id": 0, "fuzzy": 0, "none": 0}
    out_rows = []
    for say in says:
        chat = str(say.get("target") or "")
        ts = float(say.get("ts") or 0)
        text = str(say.get("text") or "")
        res = str(say.get("result") or "")
        gid = str(say.get("group_id") or "")
        qids = [int(x) for x in (say.get("queue_ids") or [])
                if str(x).strip().lstrip("-").isdigit()]

        picks = []
        joined_by = "none"
        if qids:
            idxs = [by_id[q] for q in qids if q in by_id and by_id[q] not in used]
            if idxs:
                picks = [rows[i] for i in idxs]
                used.update(idxs)
                joined_by = "queue_ids"
        if not picks and gid:
            idxs = [i for i in by_group.get((chat, gid), []) if i not in used]
            if idxs:
                picks = [rows[i] for i in idxs]
                used.update(idxs)
                joined_by = "group_id"
        if not picks and not qids and not gid:
            joined_by = "fuzzy"
            best = None
            for i, row in enumerate(rows):
                if i in used or str(row.get("chat_id") or "") != chat:
                    continue
                gap = abs(float(row.get("created_ts") or 0) - ts)
                if gap > _JOIN_WINDOW_S:
                    continue
                score = (0 if _same_text(text, row.get("preview")) else 1, gap)
                if best is None or score < best[0]:
                    best = (score, i)
            if best is not None:
                picks = [rows[best[1]]]
                used.add(best[1])

        if not picks:
            joins["none"] += 1
        else:
            joins[joined_by] = joins.get(joined_by, 0) + 1

        states = [str(p.get("state") or "") for p in picks]
        acks = [str(p.get("ack") or "") for p in picks]
        rec_ack = ""
        for p in picks:
            _r = receipts.get((chat, "".join(str(p.get("preview") or "").split())[:16]), {})
            if str((_r or {}).get("ack") or "") == "confirmed":
                rec_ack = "confirmed"
                break
        if not rec_ack:
            _r = receipts.get((chat, "".join(text.split())[:16]), {})
            rec_ack = str((_r or {}).get("ack") or "")

        if res == "blocked":
            verdict = "blocked"
        elif res in ("error", "failed") and not picks:
            verdict = "error"
        elif not picks:
            verdict = "no_queue"
        else:
            verdict = _group_verdict(states, acks, rec_ack)

        # 整条开口的送达耗时 = 第一个气泡入队 → 最后一个气泡落定。
        # 还有气泡没落定时不给数（半截的延时是假数字）。
        lat = None
        cs, ss = [], []
        for p in picks:
            try:
                if p.get("created_ts"):
                    cs.append(float(p["created_ts"]))
                if p.get("settled_ts"):
                    ss.append(float(p["settled_ts"]))
            except Exception:
                continue
        if cs and ss and len(ss) == len(picks):
            lat = round(max(ss) - min(cs), 1)

        _uq = [a for a in acks if a != "confirmed"]
        out_rows.append({
            "at": str(say.get("at") or ""), "chat": chat,
            "text": text, "why": str(say.get("why") or ""),
            "kind": str(say.get("kind") or "text"),
            "said_result": res,
            "blocked_reason": str(say.get("blocked_reason") or ""),
            "group_id": gid,
            "bubbles": len(picks),
            "join": joined_by if picks else "none",
            "queue_id": (picks[0].get("id") if picks else None),
            "queue_ids": [p.get("id") for p in picks],
            "queue_state": (states[0] if states and len(set(states)) <= 1 else ("mixed" if states else "")),
            "queue_ack": ("confirmed" if not _uq else _uq[0]),
            "why_failed": next((str(p.get("why") or "") for p in picks if str(p.get("why") or "")), ""),
            "verdict": verdict, "latency_s": lat, "receipt_ack": rec_ack,
        })

    counts = {k: 0 for k in _DELIVERY_STATES}
    by_chat = {}
    lats = []
    for r in out_rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        c = by_chat.setdefault(r["chat"], {"said": 0, "confirmed": 0, "unconfirmed": 0,
                                           "failed": 0, "no_queue": 0, "blocked": 0,
                                           "last_at": ""})
        c["said"] += 1
        if r["verdict"] in c:
            c[r["verdict"]] += 1
        c["last_at"] = max(c["last_at"], r["at"])
        if r["latency_s"] is not None:
            lats.append(r["latency_s"])
    lats.sort()
    total = len(out_rows)
    reach = counts["confirmed"] + counts["unconfirmed"]
    return {
        "ok": True,
        "n": total,
        "counts": counts,
        "reach": reach,
        "blind": counts["no_queue"] + counts["unconfirmed"],
        "median_latency_s": (lats[len(lats) // 2] if lats else None),
        "by_chat": [dict(chat=k, **v) for k, v in sorted(by_chat.items(), key=lambda kv: -kv[1]["said"])],
        # 配对质量：group_id 越多说明账本写入侧已生效；fuzzy 是老数据的兜底，
        # 这个数应该只减不增（涨了说明写入侧又漏了 group_id）。
        "join": joins,
        # 反向对账：队列里有、账本里却没有对应开口的气泡。
        # 2026-11-01 实测 4 条（全是 filehelper，其中一条 failed）—— 说明有出口
        # 不写账本。只从"她说了什么"往回查，这些气泡永远不出现在任何视图里，
        # 连同它们的失败一起被吞掉，所以出口视图必须两边都看。
        "orphans": _orphan_bubbles(rows, out_rows),
        "rows": out_rows[-80:][::-1],
    }


_DELIVERY_CACHE = {"at": 0.0, "hours": 0.0, "data": None}
_DELIVERY_TTL_S = 15.0


def delivery_audit(hours: float = 24.0, *, force: bool = False) -> dict:
    """观察窗：最近 N 小时的开口 → 队列 → 平台回执，逐条对账。

    面板每 2 秒轮询 /state，真去读库 + 读账本要 ~100ms，所以缓存 15 秒
    （对账不是秒级指标，晚 15 秒看不出来）。
    """
    now = time.time()
    if (not force and _DELIVERY_CACHE["data"] is not None
            and _DELIVERY_CACHE["hours"] == float(hours)
            and now - _DELIVERY_CACHE["at"] < _DELIVERY_TTL_S):
        return _DELIVERY_CACHE["data"]
    try:
        says = _ledger_says(hours)
        rows = _queue_rows(hours)
        receipts = _receipt_acks(hours)
    except Exception as exc:
        return {"ok": False, "error": repr(exc)[:180], "rows": []}
    res = audit_join(says, rows, receipts)
    res["hours"] = hours
    res["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _DELIVERY_CACHE.update({"at": now, "hours": float(hours), "data": res})
    return res


def outbound_view(hours: float = 24.0, *, force: bool = False) -> dict:
    """面板唯一该读的出口视图：**一次开口一行**，从「她说」走到「对方认账」。

    2026-11-01：原来面板要在三处各读一段（`ledger.says` 说、`queue` 排、`delivery` 到），
    前端自己拼。这里先把三段收成一条事件给前端用（只读、可回滚，不动写入侧）。
    等写入侧也统一成事件（said → queued → sent → confirmed 挂在同一条上），
    `ledger` / `queue` / `delivery` 三个字段一起删。

    `stage` 取值：said（只说了没进队列）/ blocked / waiting / sent / confirmed /
    unconfirmed / failed / expired / no_queue / error。`stuck` 只放需要人处理的。
    """
    d = delivery_audit(hours, force=force)
    if not d.get("ok"):
        return {"ok": False, "error": d.get("error") or "", "rows": [], "stuck": [], "counts": {}}
    labels = {}
    try:
        from db import read_gate as _rg
        labels = _rg.labels()
    except Exception:  # noqa: BLE001 - 补不到名字不影响这张表
        labels = {}
    out = []
    for r in d.get("rows") or []:
        chat = str(r.get("chat") or "")
        out.append({
            "at": r.get("at") or "",
            "chat": chat,
            "who": labels.get(chat, "") or chat,
            "text": r.get("text") or "",
            "kind": r.get("kind") or "text",
            "stage": str(r.get("verdict") or ""),
            "bubbles": int(r.get("bubbles") or 0),
            "join": str(r.get("join") or ""),
            "queue_id": r.get("queue_id"),
            "ack": r.get("receipt_ack") or r.get("queue_ack") or "",
            "latency_s": r.get("latency_s"),
            "why": (r.get("blocked_reason") or r.get("why_failed") or r.get("why") or ""),
        })
    stuck = [x for x in out if x["stage"] in ("failed", "expired", "no_queue", "unconfirmed")]
    orphans = d.get("orphans") or []
    return {"ok": True, "hours": hours, "n": len(out), "rows": out,
            "counts": d.get("counts") or {}, "stuck": stuck[:20],
            "join": d.get("join") or {},
            "orphans": orphans,
            "orphan_n": len(orphans),
            "orphan_bad": sum(1 for o in orphans if str(o.get("state") or "") in ("failed", "expired")),
            "reach": d.get("reach"), "blind": d.get("blind"),
            "median_latency_s": d.get("median_latency_s"),
            "by_chat": d.get("by_chat") or [],
            "at": d.get("at") or ""}
