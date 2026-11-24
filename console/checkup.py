# -*- coding: utf-8 -*-
"""体检（P4）：只读地把「她是不是在对的世界里说话」查一遍，给红黄绿。

每一项都是可证伪的事实：说得出数字，指得出证据，给得出修法。
不写任何业务数据；唯一的写是 data/console/ 下的缓存。
"""
from __future__ import annotations

import ctypes
import glob
import json
import os
import sqlite3
import time
from datetime import datetime

from core.infra import acl as _acl
from core.infra import senses as _senses
from core.infra import liveness  # 心跳文件名与判死口径只有一处
from core.infra import duration  # 时长只有一个单位：毫秒（UI-066）
from core.infra.duration import human, to_ms
from core.infra import tempo as _tempo  # 人味节奏只有一个来源（UI-066 乙）

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
LOGS = os.path.join(ROOT, "logs")
OUT_DIR = os.path.join(DATA, "console")

QUIET_DEFAULT = (23, 7)
WORLD_OK_S = 180.0
WORLD_WARN_S = 900.0
ACK_WARN_HOURS = 12.0
ENGINE_STALE_S = 120.0
MAIN_STALE_MS = to_ms("45s")
SPONT_MIN_GAP_S = 1200.0
WARN_LOG_WARN = 20
DISK_MIN_GB = 5.0


# ---------------------------------------------------------------- 小工具
def _now() -> float:
    return time.time()


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")


def _read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _db_rows(path: str, sql: str, args: tuple = ()) -> list:
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=3)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return []


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except Exception:  # noqa: BLE001
        return 0.0


def _tail(path: str, n: int = 400) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            raw = f.read().decode("utf-8", "replace")
        return raw.splitlines()[-n:]
    except Exception:  # noqa: BLE001
        return []


def _ledger_today() -> list[dict]:
    from core.infra import actions as _actions

    path = _actions.path()
    rows = []
    for line in _tail(path, 4000):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return rows


def _trace_today() -> list[dict]:
    path = os.path.join(DATA, "loop", "trace-%s.jsonl" % datetime.now().strftime("%Y-%m-%d"))
    rows = []
    for line in _tail(path, 400):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return rows


def _at_ts(s: str) -> float:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%H:%M:%S"):
        try:
            d = datetime.strptime(s, fmt)
            if fmt == "%H:%M:%S":
                t = datetime.now()
                d = d.replace(year=t.year, month=t.month, day=t.day)
            return d.timestamp()
        except Exception:  # noqa: BLE001
            continue
    return 0.0


def _live_account() -> str:
    best, best_at = "", ""
    for p in glob.glob(os.path.join(DATA, "platforms", "**", "account.db"), recursive=True):
        rows = _db_rows(p, "select max(created_at) from messages")
        at = str(rows[0][0] or "") if rows and rows[0] else ""
        if at > best_at:
            best, best_at = p, at
    return best


def _decrypted_latest() -> tuple[float, int]:
    latest, n = 0.0, 0
    for p in glob.glob(os.path.join(DATA, "decrypted", "**", "msg_message*.db"), recursive=True):
        if "_fts" in p or "resource" in p:
            continue
        names = _db_rows(p, "select name from sqlite_master where type='table' and name like 'Msg%'")
        for (name,) in names:
            try:
                v = _db_rows(p, 'select max(create_time) from "%s"' % name)
                if v and v[0] and v[0][0]:
                    latest = max(latest, float(v[0][0]))
                    n += 1
            except Exception:  # noqa: BLE001
                continue
    return latest, n


def _quiet_hours() -> tuple[int, int]:
    cfg = _read_json(os.path.join(ROOT, "configs", "runtime.json"), {}) or {}
    q = cfg.get("quiet_hours")
    try:
        if isinstance(q, list) and len(q) == 2:
            return int(q[0]), int(q[1])
    except Exception:  # noqa: BLE001
        pass
    return QUIET_DEFAULT


def _in_quiet(ts: float, hours: tuple[int, int]) -> bool:
    h = datetime.fromtimestamp(ts).hour
    a, b = hours
    if a == b:
        return False
    return a <= h or h < b if a > b else (a <= h < b)


TEST_TARGETS = {"wxid_a", "wxid_c", "wxid_test", "test"}


def _fold_deliveries(rows: list[dict]) -> list[dict]:
    """账本是追加式：回执补齐写的是 `kind="delivery"` 的新行，读的时候要盖回 say 行。"""
    latest: dict[str, str] = {}
    for r in rows:
        if r.get("kind") == "delivery" and r.get("turn_id"):
            latest[str(r["turn_id"])] = str(r.get("status") or "")
    if not latest:
        return rows
    out = []
    for r in rows:
        tid = str(r.get("turn_id") or "")
        if r.get("act") == "say" and tid in latest and latest[tid]:
            r = dict(r)
            r["ack"] = latest[tid]
        out.append(r)
    return out


def _real_says() -> list[dict]:
    """只算真正对活人说出去的话：排除 dry_run、自测 target、why=测试 的行。"""
    out = []
    for r in _fold_deliveries(_ledger_today()):
        if r.get("act") != "say":
            continue
        if (r.get("ack") or "") == "dry_run":
            continue
        if str(r.get("target") or "") in TEST_TARGETS:
            continue
        if str(r.get("why") or "").strip() == "测试":
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------- 检查项
def _mk(cid, title, level, line, evidence=None, hint=""):
    return {"id": cid, "title": title, "level": level, "line": line,
            "evidence": [e for e in (evidence or []) if e], "hint": hint}


def check_engine() -> dict:
    info = liveness.read("engine")
    pid = info.get("pid")
    # 2026-10-31 修：uptime_ms 是毫秒。原来直接 _now() - up（当秒减）并且 up/3600 当小时，
    # 于是「已跑 198.0 小时 / 启动于 10-23」——被放大 1000 倍。
    up = int(info.get("uptime_ms") or 0)
    started = _now() - up / 1000.0
    alive = liveness.alive("engine")  # 卡死的引擎 pid 还在，只看 pid 会报“健康”
    ev = [f"pid={pid} 活着={alive}", f"拍数={info.get('ticks')} 上次={info.get('last_tick_at')} "
          f"idle={info.get('idle')} dry_run={info.get('dry_run')}",
          f"启动于 {_hm(started)}（已跑 {up / 3_600_000:.1f} 小时）"]
    if not alive:
        return _mk("engine", "AI 引擎", "fail", f"引擎进程 {pid} 不在", ev,
                   "看 logs/supervisor.out；跑鲸鲸.bat 或 重启鲸鲸.bat")
    if info.get("dry_run"):
        return _mk("engine", "AI 引擎", "warn", "引擎在 dry_run（说的话不会真发）", ev,
                   "去掉 --dry-run 重启引擎")
    return _mk("engine", "AI 引擎", "ok", f"在跑，pid {pid}，已 {up / 3_600_000:.1f} 小时", ev)


def check_main() -> dict:
    hb = liveness.read("scheduler")
    pid = hb.get("pid")
    age_ms = liveness.age_ms("scheduler")
    alive = liveness.alive("scheduler")
    _age_text = duration.human(age_ms) if age_ms is not None else "不知道"
    ev = [f"pid={pid} 活着={alive}", f"心跳 {_age_text}前 · 主循环 {hb.get('main_loop_ticks')} 拍",
          f"当前 job={hb.get('job') or '空'}"]
    if not alive:
        return _mk("main", "主程序（调度器）", "fail", f"主程序进程 {pid} 不在", ev,
                   "跑 启动鲸鲸.bat；它会拉起 scripts/supervisor.py")
    if age_ms is not None and age_ms > MAIN_STALE_MS:
        return _mk("main", "主程序（调度器）", "warn",
                   f"心跳 {_age_text}没更新（上限 {duration.human(MAIN_STALE_MS)}）", ev,
                   "可能卡在一个长 job 上；看 logs/yinyue.log")
    return _mk("main", "主程序（调度器）", "ok", f"心跳正常（{_age_text}前）", ev)


def check_code_fresh() -> dict:
    info = liveness.read("engine")
    # 2026-10-31 修：这里原来引用了一个没定义的 up，于是这项永远 NameError → 体检里
    # 「代码与进程一致」长期失效。uptime_ms 本身就是毫秒，除以 1000 就是秒。
    up = int(info.get("uptime_ms") or 0)
    started = _now() - up / 1000.0
    newest, newest_f = 0.0, ""
    for p in glob.glob(os.path.join(ROOT, "loop", "*.py")):
        m = _mtime(p)
        if m > newest:
            newest, newest_f = m, os.path.basename(p)
    ev = [f"引擎启动于 {_hm(started)}", f"loop/ 最新改动 {newest_f} @ {_hm(newest) if newest else '—'}"]
    if newest and newest > started + 5:
        return _mk("code", "代码与进程一致", "warn",
                   f"改过 loop/ 但引擎是旧的（{newest_f} 比进程新）", ev,
                   "写 logs/loop.stop 让调度器在 60 秒内重拉引擎")
    return _mk("code", "代码与进程一致", "ok", "引擎跑的是磁盘上的代码", ev)


def check_world() -> dict:
    acct = _live_account()
    dec_ts, dec_n = _decrypted_latest()
    seen = 0.0
    if acct:
        rows = _db_rows(acct, "select max(created_at) from messages")
        if rows and rows[0] and rows[0][0]:
            seen = _at_ts(str(rows[0][0])[:19])
    gap = (dec_ts - seen) if (dec_ts and seen) else 0.0
    ev = [f"她看到的最新 {_hm(seen) if seen else '—'}（{os.path.basename(os.path.dirname(os.path.dirname(acct)))}）" if acct else "没有可读的 account.db",
          f"解密库最新 {_hm(dec_ts) if dec_ts else '—'}（扫到 {dec_n} 张消息表）",
          f"同步差 {gap:.0f} 秒"]
    if not seen or not dec_ts:
        return _mk("world", "世界同步", "unknown", "缺一侧数据，算不出同步差", ev, "看 data/decrypted 与 platforms 是否存在")
    if gap > WORLD_WARN_S:
        return _mk("world", "世界同步", "fail", f"世界落后 {gap/60:.0f} 分钟：她在对旧世界说话", ev,
                   "跑 python run.py sync；若反复落后，看 core/execution/gui/wechat.py 的解密刷新")
    if gap > WORLD_OK_S:
        return _mk("world", "世界同步", "warn", f"世界落后 {gap:.0f} 秒（正常应 <{WORLD_OK_S:.0f}s）", ev,
                   "观察一分钟；持续落后就跑一次 run.py sync")
    return _mk("world", "世界同步", "ok", f"同步差 {gap:.0f} 秒（≤{WORLD_OK_S:.0f}s）", ev)


def check_ack() -> dict:
    rows = _real_says()
    cutoff = _now() - ACK_WARN_HOURS * 3600
    recent = [r for r in rows if _at_ts(str(r.get("at") or "")) >= cutoff]
    verifiable = [r for r in recent if (r.get("ack") or "") in ("confirmed", "unconfirmed")]
    legacy = [r for r in recent if (r.get("ack") or "") not in ("confirmed", "unconfirmed")
              and (r.get("result") or "") not in ("blocked",)]
    conf = sum(1 for r in verifiable if (r.get("ack") or "") == "confirmed")
    unc = sum(1 for r in verifiable if (r.get("ack") or "") == "unconfirmed")
    blocked = sum(1 for r in recent if (r.get("result") or "") == "blocked")
    ev = [f"最近 {ACK_WARN_HOURS:.0f} 小时开口 {len(recent)} 次：confirmed {conf} / unconfirmed {unc} "
          f"/ blocked {blocked}" +
          (f" / 无回执字段 {len(legacy)}（回执自证上线前的老账）" if legacy else "")]
    if not verifiable:
        return _mk("ack", "出口回执", "unknown",
                   f"最近 {ACK_WARN_HOURS:.0f} 小时没有可验证的回执", ev,
                   "她说下一句话之后再来看；或引擎还没重启到带回执自证的版本")
    if conf == 0:
        return _mk("ack", "出口回执", "warn", f"{len(verifiable)} 条可验的全是 unconfirmed", ev,
                   "看 logs/silent_warnings.log 有没有 wx_send_ack_lookup_error / database disk image is malformed")
    return _mk("ack", "出口回执", "ok", f"{conf}/{len(verifiable)} 条已被证实", ev)


def _spontaneous_turns() -> tuple[set, int]:
    """今天哪些 turn 是"她自己想开口"（wake.spontaneous）。

    返回 (自发 turn_id 集合, 有 wake 判定的拍数)。夜里回人是允许的，只有自发才该被静默挡住；
    拍数为 0 说明今天的记录还不带这个标记（旧账/刚重启），只能报 unknown。
    """
    out: set = set()
    known = 0
    try:
        for rec in _trace_today():
            wake = rec.get("wake") or {}
            if "spontaneous" not in wake:
                continue
            known += 1
            if wake.get("spontaneous"):
                out.add(str(rec.get("turn_id") or ""))
    except Exception:  # noqa: BLE001
        pass
    return out, known


def check_quiet() -> dict:
    q = _quiet_hours()
    rows = _real_says()
    spont, known = _spontaneous_turns()
    in_window = [r for r in rows if _in_quiet(_at_ts(str(r.get("at") or "")), q)]
    # 只把"自发"算违规：夜里被主人/群友叫醒回话不算。没有 trace 就无法判定，只能报 unknown
    if known == 0:
        ev0 = [f"安静时段 {q[0]}–{q[1]} 点", f"时段内开口 {len(in_window)} 次，但没有可判定的 trace（旧账或刚重启）"]
        return _mk("quiet", "夜间静默", "unknown", "判不了：今天没有带 wake 标记的记录", ev0,
                   "等引擎跑一两拍再看（trace 里现在会写 spontaneous 布尔）")
    bad = [r for r in in_window if str(r.get("turn_id") or "") in spont]
    ev = [f"安静时段 {q[0]}–{q[1]} 点",
          f"时段内开口 {len(in_window)} 次，其中自发的 {len(bad)} 次（不含 dry_run）"]
    if bad:
        ev += [f"{r.get('at')} → {r.get('target')}" for r in bad[:3]]
        return _mk("quiet", "夜间静默", "fail", f"安静时段内说了 {len(bad)} 句", ev,
                   "看 loop/selfstate.py 的 in_quiet_hours 是否生效（引擎要重启）")
    return _mk("quiet", "夜间静默", "ok", "安静时段里一句没说", ev)


def check_spont() -> dict:
    rows = _real_says()
    # 2026-10-27: 只有"她自己想开口"的拍才受 20 分钟退避约束；
    # 回人（有人问、她答）本来就可能是几秒一条，算进来会永远误报。
    spont, known = _spontaneous_turns()
    if known:
        rows = [r for r in rows if str(r.get("turn_id") or "") in spont]
    # 一次开口会拆成 1–3 条气泡、落在同一秒：按 turn_id 去重，否则"最短间隔"永远是 0
    per_turn: dict[str, float] = {}
    for r in rows:
        t = _at_ts(str(r.get("at") or ""))
        if not t:
            continue
        key = str(r.get("turn_id") or ("%s|%s" % (r.get("at"), r.get("target"))))
        if key not in per_turn or t < per_turn[key]:
            per_turn[key] = t
    # 只看最近 6 小时：早于这版引擎的旧账（还没退避逻辑）不该算在今天的节奏里
    horizon = _now() - 6 * 3600
    ts = sorted(t for t in per_turn.values() if t >= horizon)
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    tight = [g for g in gaps if 0 < g < SPONT_MIN_GAP_S]
    ev = [f"最近 6 小时开口 {len(ts)} 次（今天共 {len(per_turn)} 次）",
          f"最短间隔 {min(gaps)/60:.0f} 分钟" if gaps else "最近 6 小时最多一次",
          f"小于 {SPONT_MIN_GAP_S/60:.0f} 分钟的间隔 {len(tight)} 处"]
    if len(ts) < 2:
        return _mk("spont", "自发节奏", "unknown", "最近 6 小时开口不足两次，看不出节奏", ev)
    if tight:
        return _mk("spont", "自发节奏", "warn", f"有 {len(tight)} 次间隔短于 {SPONT_MIN_GAP_S/60:.0f} 分钟", ev,
                   "看 loop/selfstate.py 的 _spont_gap（没回话会退避到 40/80/160/240 分钟）")
    return _mk("spont", "自发节奏", "ok", "没有连续追问", ev)


def check_promise() -> dict:
    try:
        from loop import ledger as _lg
        import sys
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        items = _lg.open_promises(limit=20)
    except Exception as exc:  # noqa: BLE001
        return _mk("promise", "承诺账", "unknown", "读不出来", [repr(exc)], "看 loop/ledger.py 的 open_promises")
    ev = [f"还欠着 {len(items)} 条"]
    if items:
        ev += ["%s｜%s：%s" % (str(i.get("at") or "")[5:16], i.get("who") or i.get("target") or "?", str(i.get("what") or "")[:40])
               for i in items[:3]]
        oldest = min((_at_ts(str(i.get("at") or "")) for i in items), default=0)
        age_d = (_now() - oldest) / 86400 if oldest else 0
        if age_d > 2:
            return _mk("promise", "承诺账", "warn", f"最老的一条挂了 {age_d:.1f} 天", ev, "面板「试」不出；去「律」不重要——直接跟她说，或改 prompt 提醒")
        return _mk("promise", "承诺账", "ok", f"{len(items)} 条在账上，还没过期", ev)
    return _mk("promise", "承诺账", "ok", "没有未销账的承诺", ev)


def check_trace() -> dict:
    rows = _trace_today()
    if not rows:
        return _mk("trace", "记录层", "unknown", "今天没有 trace（引擎没跑过，或没重启到带记录层的版本）", [],
                   "重启引擎一次，下一拍就会落盘")
    last = rows[-1]
    last_at = _at_ts(str(last.get("at") or "")) or _mtime(os.path.join(DATA, "loop", "trace-%s.jsonl" % datetime.now().strftime("%Y-%m-%d")))
    age_min = (_now() - last_at) / 60 if last_at else 0
    errs = 0
    for r in rows[-50:]:
        if (r.get("notes") or {}).get("llm_error"):
            errs += 1
        for c in r.get("calls") or []:
            if c.get("error"):
                errs += 1
    avg_tok = sum((c.get("tokens") or 0) for r in rows[-20:] for c in (r.get("calls") or [])) / max(1, len(rows[-20:]))
    ev = [f"今天 {len(rows)} 拍，最后一拍 {age_min:.0f} 分钟前（{last.get('at')}）",
          f"最近 50 拍里 LLM 出错 {errs} 次", f"最近 20 拍平均 {avg_tok:.0f} tokens/拍"]
    if errs:
        return _mk("trace", "记录层", "warn", f"最近 50 拍里有 {errs} 次 LLM 报错", ev, "看 trace 里 calls[].error 具体是什么")
    if age_min > 180:
        return _mk("trace", "记录层", "warn", f"最后一拍在 {age_min/60:.1f} 小时前", ev,
                   "她可能一直没醒（世界没变也没自发）；看「视界」的世界新鲜度")
    return _mk("trace", "记录层", "ok", f"{len(rows)} 拍，最近 {age_min:.0f} 分钟前", ev)


def check_warnings() -> dict:
    path = os.path.join(LOGS, "silent_warnings.log")
    lines = _tail(path, 2000)
    cutoff = _now() - 3600
    recent = []
    for ln in lines:
        if not ln.strip():
            continue
        try:
            ts = datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:  # noqa: BLE001
            continue
        if ts >= cutoff:
            recent.append(ln)
    ev = [f"最近 1 小时 {len(recent)} 条", f"文件 {_hm(_mtime(path)) if _mtime(path) else '—'} 最后更新"]
    if recent:
        ev += [ln.strip()[:150] for ln in recent[-3:]]
    if len(recent) >= WARN_LOG_WARN:
        return _mk("warnlog", "告警日志", "warn", f"最近 1 小时 {len(recent)} 条静默告警", ev,
                   "逐条看 logs/silent_warnings.log；同类反复出现就是真 bug")
    return _mk("warnlog", "告警日志", "ok", f"最近 1 小时 {len(recent)} 条（阈值 {WARN_LOG_WARN}）", ev)


def check_disk() -> dict:
    try:
        free = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(os.path.splitdrive(ROOT)[0] + "\\"), None, ctypes.byref(free), None)
        gb = free.value / (1024 ** 3)
    except Exception:  # noqa: BLE001
        gb = -1
    size = 0
    for base in (os.path.join(DATA, "loop"), os.path.join(DATA, "console"), os.path.join(ROOT, "backups")):
        for p in glob.glob(os.path.join(base, "**", "*"), recursive=True):
            if os.path.isfile(p):
                try:
                    size += os.path.getsize(p)
                except Exception:  # noqa: BLE001
                    pass
    ev = [f"C: 可用 {gb:.1f} GB" if gb >= 0 else "读不出磁盘空间",
          f"data/loop + data/console + backups ≈ {size/1024/1024:.0f} MB"]
    if gb >= 0 and gb < DISK_MIN_GB:
        return _mk("disk", "磁盘", "warn", f"只剩 {gb:.1f} GB", ev, "清 backups/ 与 .test_tmp/")
    return _mk("disk", "磁盘", "ok", f"空间够（可用 {gb:.1f} GB）" if gb >= 0 else "空间未知", ev)


def check_senses() -> dict:
    """她「看得见什么、动得了什么手」——直接读平台声明与探针，不在这里另立判据。"""
    try:
        rows = _senses.table()
    except Exception as exc:  # noqa: BLE001
        return _mk("senses", "她的眼和手", "unknown", "这项自己崩了", [repr(exc)], "看 traceback")
    wired = [r for r in rows if r["shape"] != "none"]
    if not wired:
        return _mk("senses", "她的眼和手", "unknown", "一个平台都还没接", [], "接线后这里会自动出来")
    line = "；".join(r["line"] for r in wired)
    blind = [r["key"] for r in wired if r["evidence"] == "unknown"]
    if blind:
        return _mk("senses", "她的眼和手", "unknown", line, blind, "这些平台的感知没有可查证据，接上探针")
    stale = [r["key"] for r in wired if r["evidence"] == "stale"]
    if stale:
        return _mk("senses", "她的眼和手", "warn", line, stale, "这些平台现在可能看不见：心跳不新鲜或没在听")
    return _mk("senses", "她的眼和手", "ok", line, [r["key"] for r in wired])


def check_acl() -> dict:
    """谁能读她的卡：icacls 说话，不看 POSIX mode（chmod 在 Windows 上是空操作）。"""
    try:
        rep = _acl.report()
    except Exception as exc:  # noqa: BLE001
        return _mk("acl", "她的卡谁能读", "unknown", "这项自己崩了", [repr(exc)], "看 traceback")
    if rep["level"] == "unknown":
        return _mk("acl", "她的卡谁能读", "unknown", rep["line"], rep["unknown"],
                   "这些目录查不到 ACL（不存在，或 icacls 不可用）")
    if rep["level"] == "fail":
        return _mk("acl", "她的卡谁能读", "fail", rep["line"], rep["strangers"],
                   "有别人能读她的卡：跑 python -m core.infra.acl 收紧")
    if rep["level"] == "warn":
        return _mk("acl", "她的卡谁能读", "warn", rep["line"], rep["inherited"],
                   "还继承着上层 ACL，没断继承")
    return _mk("acl", "她的卡谁能读", "ok", rep["line"], rep["paths"])


def check_tempo() -> dict:
    """她的人味节奏：一张表说了算，改配置就能调她（UI-066 乙）。"""
    try:
        rows = _tempo.table()
    except Exception as exc:  # noqa: BLE001 - 这项自己崩了也得说出来，不许当没事
        return _mk("tempo", "她的节奏旋钮", "unknown", "这项自己崩了", [repr(exc)],
                   "看 core/infra/tempo.py")
    line = "；".join("%s=%s" % (r["key"], r["human"]) for r in rows)
    changed = [r["key"] for r in rows if r["overridden"]]
    if changed:
        return _mk("tempo", "她的节奏旋钮", "ok", line,
                   ["被改过：" + "、".join(changed)],
                   "改 index 里的 tempo 段可以恢复默认；不用改代码")
    return _mk("tempo", "她的节奏旋钮", "ok", line, [r["key"] for r in rows],
               "调 configs/runtime.json 的 tempo 段就能调她，不用改代码")


def check_roster():
    """名单旁路表（CB-139 / BR-069）：每群多少人、快照多旧、最近判成「不在册」几次。"""
    try:
        from core.context import room_members as _rm

        con = _rm.connect()
        try:
            rooms = con.execute(
                "select chat_id, synced_at,"
                " (select count(*) from room_member m"
                "  where m.chat_id = s.chat_id and m.still_listed = 1)"
                " from room_sync s order by chat_id"
            ).fetchall()
        finally:
            con.close()
        outs = _rm.recent_outs(hours=24, limit=3)
        n_out = _rm.count_outs(hours=24)
        fresh_ok = _rm.fresh(rooms[0][0], max_age_ms=_rm.MAX_AGE_MS) if rooms else False
    except Exception as exc:  # noqa: BLE001 - 读不到就报 unknown，不当 ok
        return _mk("roster", "群里的名单", "unknown", "读不到旁路表：%r" % (exc,))
    if not rooms:
        return _mk("roster", "群里的名单", "warn", "旁路表还是空的",
                   hint="跑 scripts/sync_wx_roster.py 同步一次")
    total = sum(int(n or 0) for _c, _s, n in rooms)
    newest = max([str(s or "") for _c, s, _n in rooms] or [""])
    line = "%d 个群 / %d 人 · 最近同步 %s · 24 小时内判成不在册 %d 次" % (
        len(rooms), total, newest or "?", n_out)
    ev = ["%s：%d 人（%s）" % (c, int(n or 0), s or "?") for c, s, n in rooms[:4]]
    ev += ["%s → 不在册：%s" % (p, w) for _c, p, w, _a in outs[:3]]
    return _mk("roster", "群里的名单", "ok" if fresh_ok else "warn", line, evidence=ev,
               hint="" if fresh_ok else "快照比 %d 小时旧；她此刻「在不在」只能说不准" % (_rm.MAX_AGE_MS // 3600000))


def check_delivery() -> dict:
    """说出去的话，到底到没到（观察窗对账，2026-11-01）。

    没有这项的时候，"12 条全失败她还以为发出去了"只能靠人翻库发现。
    """
    try:
        import importlib
        import sys as _sys
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        ext = importlib.import_module("collector_ext")
        d = ext.delivery_audit(24.0, force=True)
    except Exception as exc:  # noqa: BLE001
        return _mk("delivery", "说出去的话到没到", "unknown", "对账跑不起来",
                   [repr(exc)[:140]], "看 console/collector_ext.py")
    if not d.get("ok"):
        return _mk("delivery", "说出去的话到没到", "unknown", "对账读不出来",
                   [str(d.get("error") or "")[:140]], "world.db 或账本读不到")
    c = d.get("counts") or {}
    n = int(d.get("n") or 0)
    ok_n = int(c.get("confirmed") or 0)
    blind = int(c.get("unconfirmed") or 0)
    gone = int(c.get("no_queue") or 0)
    blocked = int(c.get("blocked") or 0)
    bad = int(c.get("failed") or 0) + int(c.get("expired") or 0)
    # 「说了但没落地」的分母：扣掉被拦（她本来就没说）和出错（没进队列的重试）
    said = max(0, n - blocked)
    rate = (ok_n / said) if said else 1.0
    ev = ["近 24 小时：开口 %d，平台认账 %d，没回来认账 %d，没进队列 %d，被拦 %d%s"
          % (n, ok_n, blind, gone, blocked,
             ("，失败/过期 %d" % bad) if bad else "")]
    lat = d.get("median_latency_s")
    if lat is not None:
        ev.append("中位送出用时 %s 秒" % lat)
    whys = {}
    for r in (d.get("rows") or []):
        if r.get("verdict") in ("failed", "expired", "unconfirmed"):
            w = str(r.get("why_failed") or r.get("blocked_reason") or "")
            if w:
                whys[w] = whys.get(w, 0) + 1
    for w, k in sorted(whys.items(), key=lambda kv: -kv[1])[:3]:
        ev.append("%d 条：%s" % (k, w[:90]))
    if said and rate < 0.40 and (bad or blind >= 3):
        level, line = "fail", "说出去了，但四成以上没等到平台认账——她以为发了，其实没到"
    elif said and (rate < 0.60 or blind >= 5 or bad):
        level, line = "warn", "送达只有 %.0f%%，有几条卡在「发了但没确认」" % (rate * 100)
    else:
        level, line = "ok", "近 24 小时 %d 次开口，认账 %d 次" % (n, ok_n)
    return _mk("delivery", "说出去的话到没到", level, line, ev,
               "" if level == "ok" else "面板「心」页的「对账」一段有逐条明细；「没回来认账」多半是微信窗口没激活或锁屏")


CHECKS = [check_engine, check_main, check_code_fresh, check_world, check_ack,
          check_quiet, check_spont, check_promise, check_trace, check_warnings, check_disk,
          check_senses, check_acl, check_tempo, check_roster, check_delivery]

_ORDER = {"fail": 0, "warn": 1, "unknown": 2, "ok": 3}


def run() -> dict:
    out = []
    for fn in CHECKS:
        try:
            out.append(fn())
        except Exception as exc:  # noqa: BLE001
            out.append(_mk(fn.__name__, fn.__name__, "unknown", "这项自己崩了", [repr(exc)], "看 traceback"))
    n = {"ok": 0, "warn": 0, "fail": 0, "unknown": 0}
    for c in out:
        n[c["level"]] = n.get(c["level"], 0) + 1
    level = "fail" if n["fail"] else ("warn" if n["warn"] else ("unknown" if n["unknown"] == len(out) else "ok"))
    head = {"fail": "有毛病，按下面红条修", "warn": "能跑，但有几处要看",
            "unknown": "大部分查不出来（服务没起？）", "ok": "健康"}[level]
    return {"ok": True, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "level": level,
            "headline": head, "counts": n,
            "checks": sorted(out, key=lambda c: _ORDER.get(c["level"], 9))}
