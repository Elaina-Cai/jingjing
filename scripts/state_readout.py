"""状态驱动行为 · 读数板（试验配套，只读）。

施工单：docs/状态驱动行为_试验_施工单.md §4

用法：python scripts/state_readout.py
读 logs/ 下的 state_driven_call 日志，给三个读数：
  ① speak_render      : corr(pressure, output_chars)    期望 < 0
  ② proactive_decision: 抑制率 vs pressure 分桶        期望随 pressure 上升
  ③ 样本概况         : 数据不足时明说“读数不足”，不猜

纪律：只读日志；不连库、不发送、不调模型。分母为 0 → n/a。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re

ANSI = re.compile(r"\x1b\[[0-9;]*m")
KV = re.compile(r"([a-z_]+)=([^\s\]]+)")


#: 关心的字段（两个 sink 共用）
_KEYS = ("kind", "pressure", "knob_max_tokens", "knob_proactive_prob",
         "output_chars", "was_proactive")


def parse_line(line: str) -> dict | None:
    """支持两种 sink 格式：

    ① JSON 行（``yinyue.log``）：``{"event": "state_driven_call", "pressure": ...}``
    ② structlog 文本（``launcher_service.log``，可能带 ANSI）：``... pressure=0.5 ...``
    """
    if "state_driven_call" not in line:
        return None

    stripped = line.strip()
    if stripped.startswith("{"):
        try:
            d = json.loads(stripped)
        except Exception:
            d = None
        if isinstance(d, dict) and str(d.get("event")) == "state_driven_call":
            got = {k: str(d.get(k)) for k in _KEYS if d.get(k) is not None}
            if str(d.get("timestamp")):
                got["_ts"] = str(d["timestamp"])
            return got if "kind" in got else None

    clean = ANSI.sub("", line)
    if "state_driven_call" not in clean:
        return None
    out: dict = {}
    for k, v in KV.findall(clean):
        if k in _KEYS:
            out[k] = v
    m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", clean)
    if m:
        out["_ts"] = m.group(1)
    return out if "kind" in out else None


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dirs", default="logs")
    args = ap.parse_args()

    seen: dict = {}     # 去重：两个 sink 写同一条日志；优先保留带时间戳的那份
    scanned = 0
    for root, _d, files in os.walk(args.dirs):
        for name in files:
            p = os.path.join(root, name)
            try:
                txt = open(p, "rb").read().decode("utf-8", "replace")
            except Exception:
                continue
            scanned += 1
            for line in txt.split("\n"):
                r = parse_line(line)
                if r:
                    sig = tuple(str(r.get(k)) for k in _KEYS)
                    prev = seen.get(sig)
                    if prev is not None and (prev.get("_ts") or not r.get("_ts")):
                        continue
                    seen[sig] = r

    rows = list(seen.values())
    print("state_readout: scanned=%d files, state_driven_call rows=%d" % (scanned, len(rows)))
    if not rows:
        print("  读数不足：还没有 state_driven_call 日志（服务重启后才会产生）。")
        return 0

    kinds: dict[str, int] = {}
    for r in rows:
        kinds[str(r.get("kind"))] = kinds.get(str(r.get("kind")), 0) + 1
    print("  kind 分布:", kinds)

    # ① speak_render: pressure vs output_chars
    xs, ys = [], []
    for r in rows:
        if r.get("kind") != "speak_render":
            continue
        try:
            xs.append(float(r.get("pressure", "0")))
            ys.append(float(r.get("output_chars", "0")))
        except ValueError:
            continue
    c = pearson(xs, ys)
    print()
    print("① speak_render: n=%d" % len(xs))
    if c is None:
        print("   corr = n/a（样本不足或方差为 0）")
    else:
        print("   corr(pressure, output_chars) = %+.3f  （期望 < 0）" % c)

    # ② proactive_decision: 分桶抑制率
    buckets: dict[int, list[bool]] = {}
    for r in rows:
        if r.get("kind") != "proactive_decision":
            continue
        try:
            p = float(r.get("pressure", "0"))
        except ValueError:
            continue
        b = min(4, int(p * 5))  # 0~4 五档
        buckets.setdefault(b, []).append(str(r.get("was_proactive", "True")) == "True")
    print()
    print("② proactive_decision: n=%d" % sum(len(v) for v in buckets.values()))
    if not buckets:
        print("   n/a")
    for b in sorted(buckets):
        v = buckets[b]
        sup = sum(1 for x in v if not x) / len(v)
        print("   pressure [%.1f,%.1f)  n=%-4d 抑制率=%.0f%%" % (b / 5, (b + 1) / 5, len(v), sup * 100))

    # ③ 按小时分桶（时间/节律维度的核心读数：看深夜是否更不主动）
    # 注意：日志时间戳是 UTC，**必须转本地**，否则"深夜"会错位 8 小时。
    hourly: dict[int, list[bool]] = {}
    for r in rows:
        if r.get("kind") != "proactive_decision":
            continue
        ts = str(r.get("_ts") or "")
        if not ts:
            continue
        try:
            from datetime import datetime, timezone

            hour = datetime.fromisoformat(ts[:19]).replace(
                tzinfo=timezone.utc).astimezone().hour
        except Exception:
            m = re.search(r"T(\d{2}):", ts)
            if not m:
                continue
            hour = int(m.group(1))
        hourly.setdefault(hour, []).append(
            str(r.get("was_proactive", "True")) == "True")
    if hourly:
        print()
        print("③ proactive_decision 按小时（本地）：")
        for h in sorted(hourly):
            v = hourly[h]
            sup = sum(1 for x in v if not x) / len(v)
            print("   %02d:00  n=%-4d 抑制率=%3.0f%%  %s" % (h, len(v), sup * 100, "#" * int(sup * 20)))

    print()
    print("提示：读数只能证明“参数真的生效”；“像不像活的”还需人工盲评（施工单 §4 第 4 条）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
