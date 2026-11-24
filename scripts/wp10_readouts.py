#!/usr/bin/env python
"""WP-10 退役触发读数日报（只读）。

逐行检查《当断则断_执行手册》WP-10 10.1 的退役触发器，输出 READY / BLOCKED
以及最早可执行日期，供每日巡检：

  python scripts/wp10_readouts.py

数据源：
  * outbound_audit（data/yinyue_meta.db，只读）——出站闸门命中（repeat_fingerprint /\n    internal_lexicon 两条判定已退役，不再有命中）；
  * logs/*.log —— self_voice_rewritten / self_voice_rewrite_failed 事故与改写率；
  * feedback_samples（只读）—— correction 样本清单（归属事故口径由主人确认）；
  * .env —— YINYUE_FEATURE_THREADS 模式。

本脚本不改任何文件、不写库、不发消息。
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "yinyue_meta.db"
WINDOW_DAYS = 14


def _parse(value):
    if not value:
        return None
    text = str(value).replace("T", " ").replace("Z", "")
    text = text.split(".")[0].split("+")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _self_voice():
    counts = {"self_voice_rewritten": 0, "self_voice_rewrite_failed": 0}
    last = {}
    for path in glob.glob(str(ROOT / "logs" / "*")):
        if not os.path.isfile(path):
            continue
        try:
            lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
        except OSError:
            continue
        for line in lines:
            for key in counts:
                if key in line:
                    counts[key] += 1
                    try:
                        row = json.loads(line)
                        ts = str(row.get("timestamp") or "")
                        if ts and (key not in last or ts > last[key]):
                            last[key] = ts
                    except Exception:  # noqa: BLE001
                        pass
    return counts, last


def _feedback():
    if not DB.exists():
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
        rows = con.execute(
            "select id,created_at,source,chat_type,sample_text,reply_preview,"
            "metadata_json from feedback_samples order by id desc"
        ).fetchall()
        con.close()
        return rows
    except Exception:  # noqa: BLE001
        return []


def _threads_mode():
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "YINYUE_FEATURE_THREADS" in line:
                return line.strip()
    return "(未设置)"


ATTRIBUTION_MARKERS = ("不是我说的", "不是我说", "记错", "谁说的", "对谁说", "不是我")


def _window_hits(items):
    cutoff = datetime.now() - timedelta(days=WINDOW_DAYS)
    recent = [x for x in items if x[0] and x[0] >= cutoff]
    if not recent:
        return "READY", "近 %d 天 0 次" % WINDOW_DAYS
    last = max(x[0] for x in recent)
    return "BLOCKED", "近 %d 天 %d 次；最后 %s -> 最早可删 %s" % (
        WINDOW_DAYS, len(recent), last, (last + timedelta(days=WINDOW_DAYS)).date(),
    )


def _attribution_incidents():
    out = []
    for r in _feedback():
        if str(r[2]) != "correction":
            continue
        text = str(r[4] or "")
        if any(marker in text for marker in ATTRIBUTION_MARKERS):
            out.append((_parse(r[1]), "id=%s %s" % (
                r[0], text[:70].replace(chr(10), " "),
            )))
    return out


def _self_contract_incidents():
    try:
        from core.context.self_contract import reply_speaks_of_self_in_third_person
    except Exception:  # noqa: BLE001 - 口径库不可用时不判定
        return []
    out = []
    for r in _feedback():
        reply = str(r[5] or "")
        sample = str(r[4] or "")
        hit = bool(reply) and reply_speaks_of_self_in_third_person(reply)
        if not hit and "[引用 鲸鲸" in sample:
            hit = reply_speaks_of_self_in_third_person(sample)
        if hit:
            out.append((_parse(r[1]), "id=%s %s" % (
                r[0], (reply or sample)[:70].replace(chr(10), " "),
            )))
    return out


def main() -> int:
    print("WP-10 退役触发读数日报（只读）  %s" % datetime.now().strftime("%Y-%m-%d %H:%M"))

    attr = _attribution_incidents()
    status, note = _window_hits(attr)
    print("\n[1] [消息归属] 三条硬规则 / 归属事故: %s\n    %s" % (status, note))
    for at, desc in attr[-5:]:
        print("    %s | %s" % (at, desc))

    print("\n[2][3] REPEAT_GUARD / internal_lexicon: 已退役（2026-10-24）——"
          "近重复与用语词表判定随 core.speech.repeat_guard / guards 的两个语义检查一起删除。")

    counts, last = _self_voice()
    print("\n[4] SELF_CONTRACT: 已退役（2026-10-10，保留出口守卫）；观察 self_voice_rewritten=%d failed=%d" % (
        counts["self_voice_rewritten"], counts["self_voice_rewrite_failed"],
    ))
    print("    最后事件: rewritten=%s failed=%s" % (
        last.get("self_voice_rewritten", "无"), last.get("self_voice_rewrite_failed", "无"),
    ))
    events = []
    for key in ("self_voice_rewritten", "self_voice_rewrite_failed"):
        if last.get(key):
            at = _parse(last[key])
            if at:
                events.append((at, key))
    events.extend(x for x in _self_contract_incidents() if x[0])
    status, note = _window_hits(events)
    print("    口径: 回复或引用正文命中生产自指谓词即事故；状态 %s（%s）" % (status, note))
    for at, desc in events[-5:]:
        print("    %s | %s" % (at, desc))
    print("    feedback correction 清单见下（口径外的纠正仅作参考）")

    corrections = [r for r in _feedback() if str(r[2]) == "correction"]
    print("\n[1/4] feedback correction 样本（近 14 天 %d 条）:" % len(corrections))
    for r in corrections[:12]:
        at = _parse(r[1])
        if at and at < datetime.now() - timedelta(days=WINDOW_DAYS):
            continue
        print("    id=%s at=%s %s | %s" % (
            r[0], r[1], r[3], str(r[4])[:60].replace(chr(10), " "),
        ))
    print("    （归属类纠正口径需主人确认；[1] 引用解析读数跑 scripts/audit_attribution_quotes.py --days 14）")

    print("\n[5] 局况块 / THREADS: %s" % _threads_mode())
    if "on" not in _threads_mode().lower():
        print("    BLOCKED：THREADS 非 on / X1-X8 触发器未达")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
