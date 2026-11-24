"""重启后看一眼：证据编号这条链到底通没通（只读，不改任何东西）。

用法：.venv\\Scripts\\python.exe scripts\\verify_evidence_chain.py
判据来源：2026-11-21 排查「认知链一次没启动」时定下的四格。
"""
from __future__ import annotations

import glob
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _ro(path):
    return sqlite3.connect("file:%s?mode=ro" % str(path).replace("\\", "/"), uri=True)


def check_platform_msg_id():
    with _ro(ROOT / "data" / "world.db") as c:
        total = c.execute("select count(*) from inbound_event").fetchone()[0]
        filled = c.execute(
            "select count(*) from inbound_event where ifnull(platform_msg_id,'')<>''").fetchone()[0]
        recent_gap = c.execute(
            "select count(*) from inbound_event"
            " where ifnull(platform_msg_id,'')=''"
            " and ts_utc>=strftime('%Y-%m-%dT%H:%M:%S','now','-30 days')").fetchone()[0]
    ok = filled > 0 and recent_gap == 0
    return ok, "platform_msg_id 回填：%d / %d；近 30 天缺口：%d" % (
        filled, total, recent_gap)


# 提示词里**不会**出现「可评估的经历」这个分区名（_reshape 只拼正文、不拼标题）。
# 真正的判据是 experience_commit.render 的第一句：评价的触发面含理解、态度、情绪与意图，
# 而寒暄收尾不评，所以首行以「当这句话改变了……」开头；2026-11-21 用错的标记查了 300 份
# payload，差点把「0 次」当成「从没给过评价机会」——其实只有本进程的 27 拍是有效样本。
_OFFER_LINE = "当这句话改变了我对局面的理解、对某人的态度、我自己的情绪或下一步意图时用 evaluate_experience"
# 当前生产进程还跑旧文案；重启窗口期两种都应该算“评价机会已出现”。
_LEGACY_OFFER_LINE = "仅在相关经历改变了理解时用 evaluate_experience"


def _process_start_epoch() -> float:
    try:
        # 作为脚本直跑时 sys.path[0] 是 scripts/，仓库根要自己补上
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from core.infra import liveness

        beat = liveness.read("scheduler")
        return float(beat.get("started_at_ms") or 0) / 1000.0
    except Exception:
        return 0.0


def _process_start() -> str:
    sec = _process_start_epoch()
    return datetime.fromtimestamp(sec).strftime("%Y-%m-%dT%H:%M") if sec else ""


def _process_start_utc() -> str:
    """库里的 committed_at/updated_at 是 UTC；拿本地时间去比会整条漏掉（2026-11-24 实测误报 0）。"""
    sec = _process_start_epoch()
    return datetime.fromtimestamp(sec, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") if sec else ""


_RUNTIME_FILES = (
    "loop/run.py",
    "loop/brain.py",
    "loop/selfstate.py",
    "loop/snapshot.py",
    "core/context/appraisal.py",
    "core/context/experience_commit.py",
    "core/context/intention_close.py",
    "db/world_db.py",
)


def check_loaded_code():
    start = _process_start_epoch()
    if not start:
        return False, "读不到主进程启动时间"
    newer = [
        name for name in _RUNTIME_FILES
        if (ROOT / name).exists() and (ROOT / name).stat().st_mtime > start + 1
    ]
    return not newer, (
        "已加载当前代码" if not newer
        else "进程启动后代码又改过，尚未部署：" + "、".join(newer)
    )


def check_recent_payloads():
    start = _process_start()
    start_epoch = _process_start_epoch()
    offered_turns = set()
    files = sorted(glob.glob(str(ROOT / "data" / "loop" / "payloads" / "*.json")),
                   key=os.path.getmtime)
    for f in files:
        if start_epoch and os.path.getmtime(f) < start_epoch - 1:
            continue
        try:
            data = json.load(io.open(f, encoding="utf-8"))
        except Exception:
            print("WARN payload_unreadable", f, file=sys.stderr)
            continue
        text = (data.get("system") or "") + "\n" + (data.get("user") or "")
        if _OFFER_LINE in text or _LEGACY_OFFER_LINE in text:
            offered_turns.add(str(data.get("turn_id") or os.path.basename(f)))

    # payload 可能只覆盖部分窗口；trace 是逐拍、逐模型的完整提示词。
    trace_files = glob.glob(str(ROOT / "data" / "loop" / "trace-*.jsonl"))
    if start:
        trace_files = [f for f in trace_files if os.path.basename(f) >= "trace-" + start[:10]]
    for f in trace_files:
        try:
            handle = io.open(f, encoding="utf-8")
        except OSError:
            print("WARN trace_unreadable", f, file=sys.stderr)
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except Exception:
                    print("WARN trace_line_unreadable", f, file=sys.stderr)
                    continue
                row_ts = float(row.get("ts") or 0.0)
                if start_epoch and row_ts and row_ts < start_epoch - 1:
                    continue
                if start_epoch and not row_ts and os.path.getmtime(f) < start_epoch - 1:
                    continue
                prompt = row.get("prompt") or {}
                parts = prompt.get("parts") or ()
                text = "\n".join([
                    str(prompt.get("system") or ""),
                    str(prompt.get("user") or ""),
                    *(str(part.get("text") or "") for part in parts if isinstance(part, dict)),
                ])
                if _OFFER_LINE in text or _LEGACY_OFFER_LINE in text:
                    offered_turns.add(str(row.get("turn_id") or f + ":" + str(row_ts)))
    if not offered_turns:
        return False, "本进程（%s 起）还没有新请求，暂无可验收样本" % (start or "未知")
    return True, "本进程（%s 起）带评价机会的不同回合：%d 个" % (
        start or "最新", len(offered_turns))


def check_appraisals():
    start = _process_start_utc()
    with _ro(ROOT / "data" / "world.db") as c:
        try:
            n = c.execute("select count(*) from cognitive_state_scoped "
                          "where operation='appraise' and updated_at>=?",
                          (start,)).fetchone()[0]
        except sqlite3.OperationalError:
            n = 0
    return n > 0, "本进程（%s UTC 起）落库的评价条数：%d" % (start or "未知", n)


def check_unresolved_warnings():
    start = _process_start()
    hits = 0
    for name in ("silent_warnings.log", "yinyue.log", "yinyue.log.1"):
        p = ROOT / "logs" / name
        if not p.exists():
            continue
        try:
            text = io.open(p, encoding="utf-8", errors="replace").read()
        except Exception:
            print("WARN log_unreadable", str(p), file=sys.stderr)
            continue
        # 只看本进程启动之后的告警：测试会往同一份 silent_warnings.log 里写
        # chat-0/same 这类用例会话，把它们算进来会让这条判据永远假红。
        for line in text.splitlines():
            cut = line.find("evidence_refs_unresolved")
            if cut < 0:
                continue
            stamp = line[:19]
            try:
                when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                print("WARN bad_stamp", stamp, file=sys.stderr)
                continue
            if start and when < datetime.strptime(start, "%Y-%m-%dT%H:%M"):
                continue
            hits += 1
    return hits == 0, "evidence_refs_unresolved 告警条数：%d" % hits


def main() -> int:
    rows = [("证据编号回填", check_platform_msg_id()),
            ("评价工具出现", check_recent_payloads()),
            ("评价已落库", check_appraisals()),
            ("没有静默熄火", check_unresolved_warnings()),
            ("加载当前代码", check_loaded_code())]
    for name, (ok, line) in rows:
        print("%-6s %-10s %s" % ("PASS" if ok else "FAIL", name, line))
    print("提示：五项全 PASS 才算这条链真的通了；只有「回填」PASS 说明还要等一拍真人消息。")
    return 0 if all(ok for _, (ok, _) in rows) else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
