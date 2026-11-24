"""主动触达 · 状态闸门探针（试验配套）。

用法：
    python scripts/state_proactive_probe.py --n 12

流程：读原配置 -> 临时放宽配额 -> 连点 N 次 -> 统计 reason
      -> **finally 改回原配置**（异常也还原）。

⚠️ 被放行的尝试会**真的发出主动消息**；请选对的会话与合适的次数。

背景：存量门（cooldown / 每小时 / 每日上限）在状态闸门**之前**，
不放宽就走不到要测的地方。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8972"
BACKUP = ".tmp_proactive_orig.json"
KEYS = ("platform", "chat_id", "bot_account_id", "chat_type", "enabled",
        "allow_sticker", "max_per_hour", "max_per_day", "cooldown_minutes",
        "quiet_hours", "note")


def api(method: str, path: str, payload: dict | None = None, timeout: int = 240) -> object:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12, help="连点次数")
    args = ap.parse_args()

    targets = api("GET", "/api/proactive/targets")
    if not targets:
        print("no proactive target; abort")
        return 1
    t = targets[0]
    keep = {k: t[k] for k in KEYS}
    with open(BACKUP, "w", encoding="utf-8") as fh:
        json.dump(keep, fh, ensure_ascii=False, indent=2)
    print("target=%s" % keep["chat_id"])
    print("原配额=%s  (备份 -> %s)" % ({k: keep[k] for k in ("max_per_hour", "max_per_day", "cooldown_minutes")}, BACKUP))

    wide = dict(keep)
    wide.update({"max_per_hour": 99, "max_per_day": 99, "cooldown_minutes": 0})
    relaxed = False
    results: list[dict] = []
    try:
        api("PUT", "/api/proactive/targets", wide)
        relaxed = True
        print("已放宽配额，开始 %d 次触发：" % args.n)
        for i in range(1, args.n + 1):
            t0 = time.time()
            try:
                r = api("POST", "/api/proactive/trigger", {
                    "platform": keep["platform"], "chat_id": keep["chat_id"],
                    "bot_account_id": keep["bot_account_id"],
                    "chat_type": keep["chat_type"], "hint": "",
                })
                results.append(r)
                print("  %2d) ok=%-5s reason=%-18s (%.1fs)" % (
                    i, r.get("ok"), str(r.get("reason")), time.time() - t0))
            except Exception as e:  # noqa: BLE001
                print("  %2d) ERROR %s: %s" % (i, type(e).__name__, str(e)[:100]))
            time.sleep(0.5)
    finally:
        if relaxed:
            try:
                api("PUT", "/api/proactive/targets", keep)
                print("已改回原配额：%s" % {k: keep[k] for k in ("max_per_hour", "max_per_day", "cooldown_minutes")})
            except Exception as e:  # noqa: BLE001
                print("⚠️ 改回配额失败！请手动复原 %s：%s" % (BACKUP, e))

    if results:
        sup = sum(1 for r in results if str(r.get("reason")) == "state_suppressed")
        ok = sum(1 for r in results if r.get("ok"))
        print("=== 汇总 ===  n=%d  放行=%d  state_suppressed=%d  其他=%d  抑制率=%.0f%%" % (
            len(results), ok, sup, len(results) - ok - sup, 100.0 * sup / len(results)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
