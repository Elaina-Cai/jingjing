"""L0 体检脚本的判据单测（不碰真日志、不碰真租约）。

跑法：.venv_it\\Scripts\\python.exe -X utf8 -u tests/test_l0_glance.py
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import time
from datetime import date, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import l0_glance as G  # noqa: E402

COUNT = [0]
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    COUNT[0] += 1
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(f"{name} {detail}".strip())
    print(f"  FAIL {name} {detail}".strip())


def _tmpdir() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix="l0_", dir=str(ROOT / ".test_tmp")))


def t_signals() -> None:
    check("七个信号", len(G.SIGNALS) == 8 - 1, str(len(G.SIGNALS)))
    text = "\n".join([
        '{"event": "device_guard_shadow", "detail": {}}',
        'Input_busy 等不到手',                     # 大小写不同 -> 不命中（信号区分大小写）
        'input_busy wait_s=8',
        'input_busy UiBusy 一起出现只算一次',
        'blocked:capability',
        'correction_shadow ref=wechat#89',
        'legacy_exit_shadow platform=wechat target=a kind=say handled=0 exit_status=blocked:adapter_disabled',
        '无关的一行',
    ])
    counts = G.count_signals(text)
    check("shadow 计 1", counts["device_guard_shadow"] == 1, str(counts))
    check("input_busy 计 2（大小写敏感）", counts["input_busy"] == 2, str(counts))
    check("一行里两个别名只算一次", counts["input_busy"] == 2, str(counts["input_busy"]))
    check("blocked:capability 命中", counts["blocked_capability"] == 1, str(counts))
    check("correction 命中", counts["correction"] == 1, str(counts))
    check("legacy_exit_shadow 命中", counts["legacy_exit_shadow"] == 1, str(counts))
    check("空文本全 0", all(v == 0 for v in G.count_signals("").values()))


def t_scan_logs() -> None:
    base = _tmpdir()
    (base / "a.log").write_text('{"event": "lease_stuck"}\n', encoding="utf-8")
    (base / "b.log").write_text("input_busy\n", encoding="utf-8")
    text = G.scan_logs(log_dir=base)
    check("扫到两个文件", "lease_stuck" in text and "input_busy" in text)
    check("目录不存在返回空", G.scan_logs(log_dir=base / "nope") == "")


def t_stale_leases() -> None:
    base = _tmpdir()
    now = time.time()
    (base / "input.json").write_text(json.dumps({"resource": "input", "owner": "微信", "pid": 4242, "heartbeat_at": now}), encoding="utf-8")
    (base / "page__1.json").write_text(json.dumps({"resource": "page:1", "owner": "浏览器", "pid": 7, "heartbeat_at": now - 500}), encoding="utf-8")
    (base / "broken.json").write_text("{not json", encoding="utf-8")
    rows = G.stale_leases(base, now=now, stale_after_s=120)
    check("只报过期的那个", len(rows) == 1, str(rows))
    check("带 pid 与 owner", rows[0]["pid"] == 7 and rows[0]["owner"] == "浏览器", str(rows))
    check("age 是数字", rows[0]["age_s"] >= 400, str(rows))
    check("坏 json 不抛", True)
    check("目录不存在返回空", G.stale_leases(base / "nope") == [])


def t_days() -> None:
    today = date(2026, 10, 20)
    check("到期当天 = 0", G.days_left("2026-10-20", today=today) == 0)
    check("还有一天 = 1", G.days_left("2026-10-21", today=today) == 1)
    check("过期 = 负", G.days_left("2026-10-18", today=today) == -2)
    check("DUE_DATE 是合法日期", date.fromisoformat(G.DUE_DATE) > today)
    check("since 推已观察天数", G.days_observed(since="2026-10-13", due="2026-10-20", today=today) == 7.0)
    check("没给 since 按剩余天数推", G.days_observed(due="2026-10-27", today=today) == 0.0)


def t_shadow_mix() -> None:
    text = "\n".join([
        'legacy_exit_shadow platform=wechat target=a kind=say handled=0 exit_status=blocked:adapter_disabled',
        'legacy_exit_shadow platform=wechat target=b kind=say handled=0 exit_status=blocked:adapter_disabled',
        'legacy_exit_shadow platform=wechat target=c kind=say handled=1 exit_status=ok',
        'legacy_exit_shadow platform=wechat target=d kind=say handled=0 exit_status=deliver_failed',
        'legacy_exit_shadow platform=wechat target=e kind=say handled=0',
        '无关的一行 exit_status=ok',
    ])
    mix = G.exit_status_mix(text)
    check("分布数到三种", mix.get("blocked:adapter_disabled") == 2 and mix.get("ok") == 1
          and mix.get("deliver_failed") == 1, str(mix))
    check("没有 exit_status 的影子行不计", sum(mix.values()) == 4, str(mix))
    check("无关行的 exit_status 不计", mix.get("ok") == 1, str(mix))

    alarms = G.shadow_alarms(mix)
    check("alarms 只报 ok 与 deliver_failed", set(alarms) == {"ok", "deliver_failed"}, str(alarms))
    check("全正常时 alarms 为空", G.shadow_alarms({"blocked:adapter_disabled": 99}) == {})

    zero = {name: 0 for name in G.SIGNALS}
    level, msg = G.verdict(zero, days=7, mix={"blocked:adapter_disabled": 5})
    check("只有正常受拦 -> 通过", level == G.LEVEL_PASS, f"{level} {msg}")

    level, msg = G.verdict(zero, days=7, mix={"ok": 1})
    check("出现 ok -> 修", level == G.LEVEL_FIX, f"{level} {msg}")
    check("ok 的结论说清为什么", "WECHAT_SEND" in msg or "放行" in msg, msg)

    level, msg = G.verdict(zero, days=7, mix={"deliver_failed": 2})
    check("出现 deliver_failed -> 修", level == G.LEVEL_FIX, f"{level} {msg}")

    level, _ = G.verdict(dict(zero, lease_stuck=1), days=7, mix={"ok": 9})
    check("卡死仍然优先级最高", level == G.LEVEL_STOP, level)


def t_verdict() -> None:
    zero = {name: 0 for name in G.SIGNALS}
    level, msg = G.verdict(zero, days=7, stale=[{"resource": "input", "owner": "微信", "pid": 1, "age_s": 999}])
    check("残留租约 → 停", level == G.LEVEL_STOP, level)
    check("结论里有 pid", "pid=1" in msg, msg)

    level, _ = G.verdict(dict(zero, lease_stuck=1), days=7)
    check("lease_stuck > 0 → 停", level == G.LEVEL_STOP, level)

    level, msg = G.verdict(dict(zero, blocked_capability=3), days=7)
    check("能力不足 → 修", level == G.LEVEL_FIX, level)
    check("修的理由说到探针", "探针" in msg, msg)

    level, msg = G.verdict(zero, days=3)
    check("天数不够 → 等", level == G.LEVEL_WAIT, level)
    check("算出还差几天", "还差 4" in msg, msg)

    level, msg = G.verdict(zero, days=7)
    check("满 7 天全 0 → 过", level == G.LEVEL_PASS, msg)
    check("过的话里提 L1", "L1" in msg, msg)

    level, msg = G.verdict(dict(zero, input_busy=100), days=7)
    check("排队太多仍是过但带提醒", level == G.LEVEL_PASS and "p90" in msg, msg)


def t_main() -> None:
    base = _tmpdir()
    (base / "x.log").write_text('{"event": "device_guard_shadow"}\n', encoding="utf-8")
    locks = _tmpdir()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = G.main(["--json", "--days", "7", "--log-dir", str(base), "--lock-dir", str(locks)])
    payload = json.loads(buf.getvalue())
    check("--json 退出 0", code == 0, str(code))
    check("--json 给出结论档", payload["level"] == G.LEVEL_PASS, str(payload["level"]))
    check("--json 数到 shadow", payload["counts"]["device_guard_shadow"] == 1, str(payload["counts"]))
    check("--json 带剩余天数", payload["days_left"] == G.days_left(G.DUE_DATE), str(payload["days_left"]))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        G.main(["--days", "1", "--log-dir", str(base), "--lock-dir", str(locks)])
    text = buf.getvalue()
    check("人读输出有结论行", "结论【" in text, text[-80:])
    check("人读输出列出信号", "input_busy" in text)

    (base / "shadow.log").write_text(
        'legacy_exit_shadow platform=wechat target=a kind=say handled=1 exit_status=ok\n',
        encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        G.main(["--days", "7", "--log-dir", str(base), "--lock-dir", str(locks)])
    out = buf.getvalue()
    check("影子档 ok 出现在人读输出里", "exit_status" in out and "要看" in out, out[-160:])
    check("影子档 ok 会把结论压成修", f"结论【{G.LEVEL_FIX}】" in out, out[-120:])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        G.main(["--days", "7", "--due", "2026-10-01", "--log-dir", str(base), "--lock-dir", str(locks)])
    check("过期会喊「该看了」", "该看了" in buf.getvalue())


TESTS = (t_signals, t_scan_logs, t_stale_leases, t_days, t_verdict, t_shadow_mix, t_main)


def main() -> int:
    for fn in TESTS:
        print(f"[{fn.__name__}]")
        fn()
    print()
    print(f"RESULT {len(FAILS)} failed / {COUNT[0]} checks")
    for line in FAILS:
        print("  -", line)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
