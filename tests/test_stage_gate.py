"""U-31 放行闸：L0 → L1 的判据必须是代码，不是感觉（53 项）。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import stage_gate as G

FAILS: list[str] = []
COUNT = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if ok:
        print(f"  ok {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


class _Conn:
    def __init__(self, engine: "_Engine") -> None:
        self.engine = engine

    def execute(self, sql, params=None):
        self.engine.seen.append(" ".join(str(sql).split()))
        self.engine.params.append(dict(params or {}))
        return self.engine.queue.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _Engine:
    def __init__(self, *results) -> None:
        self.queue = list(results)
        self.seen: list[str] = []
        self.params: list[dict] = []

    def connect(self) -> _Conn:
        return _Conn(self)


def row(status="sent", target="文件传输助手", ts="2026-10-21 10:00:00", **kw):
    base = {"turn_id": "t1", "chat_id": "c1", "status": status, "kind": "say",
            "target": target, "ack": "platform_id", "reason": "", "detail": "", "ref": "", "ts": ts}
    base.update(kw)
    return base


def many(n: int, status="sent", target="文件传输助手", days: int = 7) -> list[dict]:
    out = []
    for i in range(n):
        d = 21 + (i % max(1, days))
        out.append(row(status=status, target=target, ts=f"2026-10-{d:02d} 10:{i % 60:02d}:00"))
    return out


def good_stats(**kw):
    base = {"sends": 30, "days": 7, "sent": 28, "unconfirmed": 2, "failed": 0, "duplicate": 0,
            "failed_rate": 0.0, "unconfirmed_rate": 0.066, "duplicate_rate": 0.0}
    base.update(kw)
    return base


def t_rate() -> None:
    print("[t_rate]")
    check("分母 0 是未采集", G.rate(0, 0) is None)
    check("正常比率", abs(G.rate(1, 4) - 0.25) < 1e-9)
    check("全失", G.rate(3, 3) == 1.0)


def t_self_rows() -> None:
    print("[t_self_rows]")
    rows = [row(), row(target="鲸鲸的测试群"), row(target=""), row(target="File Transfer")]
    mine = G.self_rows(rows)
    check("只留自己那批", len(mine) == 2, str([r["target"] for r in mine]))
    check("群聊被排除", all("测试群" not in r["target"] for r in mine))
    check("空 target 排除", all(r["target"] for r in mine))
    check("英文名也认", any(r["target"] == "File Transfer" for r in mine))
    check("自定义白名单", len(G.self_rows(rows, targets=("测试群",))) == 1)
    check("白名单空 = 全排除", G.self_rows(rows, targets=()) == [])


def t_owner_targets() -> None:
    print("[t_owner_targets]")
    class _Row(dict):
        @property
        def _mapping(self):
            return self

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            assert "role='owner'" in str(sql), str(sql)
            return type("R", (), {"fetchall": lambda self: [_Row(account_id="wxid_seed01"), _Row(account_id="1768136519")]})()

    class _Eng:
        def connect(self): return _Conn()

    owners = G.owner_targets(engine=_Eng())
    check("owner 从 account_registry 取", owners == ("wxid_seed01", "1768136519"), str(owners))
    names = G.self_targets(engine=_Eng())
    check("自己名单 = 自聊名 + owner", "wxid_seed01" in names and "文件传输助手" in names, str(names))
    check("不重复", len(names) == len(set(names)))

    rows = [row(), row(target="wxid_seed01"), row(target="10000000001@chatroom")]
    mine = G.self_rows(rows, targets=names)
    check("发给 owner 算自己", [r["target"] for r in mine] == ["文件传输助手", "wxid_seed01"], str([r["target"] for r in mine]))

    class _Bad:
        def connect(self): raise RuntimeError("库不在")

    check("查不到 owner 退空", G.owner_targets(engine=_Bad()) == ())


def t_day_of() -> None:
    print("[t_day_of]")
    check("datetime", G.day_of(datetime(2026, 10, 21, 9, 0)) == "2026-10-21")
    check("字符串", G.day_of("2026-10-21 09:00:00") == "2026-10-21")
    check("空", G.day_of(None) == "")
    check("乱码不给日期", G.day_of("昨天") == "")
    check("短串不给日期", G.day_of("2026-1") == "")


def t_distinct_days() -> None:
    print("[t_distinct_days]")
    rows = many(6, days=3)
    days = G.distinct_days(rows)
    check("按日期去重", len(days) == 3, str(days))
    check("升序", days == sorted(days))
    check("空日期不算一天", G.distinct_days([row(ts="")]) == [])


def t_l1_stats_shape() -> None:
    print("[t_l1_stats_shape]")
    rows = many(30) + [row(status="failed"), row(status="duplicate"), row(target="鲸鲸的测试群")]
    st = G.l1_stats(rows)
    check("分母只算自己的", st["sends"] == 32, str(st["sends"]))
    check("failed 计数", st["failed"] == 1)
    check("duplicate 计数", st["duplicate"] == 1)
    check("failed_rate", abs(st["failed_rate"] - 1 / 32) < 1e-9)
    check("unconfirmed 两种写法都算", G.l1_stats([row(status="sent_unconfirmed")])["unconfirmed"] == 1)
    check("群里的失败不算自己的", G.l1_stats([row(status="failed", target="群")])["failed"] == 0)


def t_l1_stats_empty() -> None:
    print("[t_l1_stats_empty]")
    st = G.l1_stats([])
    check("sends 0", st["sends"] == 0)
    check("days 0", st["days"] == 0)
    check("failed_rate 未采集", st["failed_rate"] is None)
    check("unconfirmed_rate 未采集", st["unconfirmed_rate"] is None)
    check("duplicate_rate 未采集", st["duplicate_rate"] is None)
    check("duplicate 未采集（不是 0）", st["duplicate"] is None)
    check("blocked 未采集（不是 0）", st["blocked"] is None)


def t_judge_pass() -> None:
    print("[t_judge_pass]")
    level, msg = G.judge_l1(good_stats(), log_counts={"lease_stuck": 0, "blocked_capability": 0}, stale=[])
    check("过了", level == G.LEVEL_PASS, msg)
    check("消息里有天数与条数", "7 天" in msg and "30 条" in msg, msg)
    check("消息里 duplicate 0", "duplicate 0" in msg, msg)


def t_judge_wait() -> None:
    print("[t_judge_wait]")
    level, msg = G.judge_l1(good_stats(days=3))
    check("天数不够是等", level == G.LEVEL_WAIT, msg)
    check("说清差几天", "3/7" in msg, msg)
    level, msg = G.judge_l1(good_stats(sends=5))
    check("条数不够是等", level == G.LEVEL_WAIT, msg)
    check("说清差几条", "5/20" in msg, msg)


def t_judge_fix() -> None:
    print("[t_judge_fix]")
    level, msg = G.judge_l1(good_stats(duplicate=1))
    check("重复发送必须修", level == G.LEVEL_FIX, msg)
    check("消息里点出 duplicate", "duplicate=1" in msg, msg)
    level, msg = G.judge_l1(good_stats(failed=3, failed_rate=0.1))
    check("失败率超标要修", level == G.LEVEL_FIX, msg)
    check("消息里点出 failed_rate", "10.0%" in msg, msg)
    level, _ = G.judge_l1(good_stats(failed=0, failed_rate=0.02))
    check("恰好 2% 不算超", level == G.LEVEL_PASS)
    level, _ = G.judge_l1(good_stats(duplicate=None))
    check("未采集不判成 0 也不判成超", level == G.LEVEL_PASS)


def t_judge_stop() -> None:
    print("[t_judge_stop]")
    level, msg = G.judge_l1(good_stats(), log_counts={"lease_stuck": 2})
    check("僵死租约一票否决", level == G.LEVEL_STOP, msg)
    level, msg = G.judge_l1(good_stats(), log_counts={"blocked_capability": 1})
    check("能力不足一票否决", level == G.LEVEL_STOP, msg)
    level, msg = G.judge_l1(good_stats(), stale=[{"resource": "input", "owner": "x"}])
    check("残留租约一票否决", level == G.LEVEL_STOP, msg)
    check("三样都写进消息", "残留租约=1" in msg, msg)
    check("停优先于修", G.judge_l1(good_stats(duplicate=9), log_counts={"lease_stuck": 1})[0] == G.LEVEL_STOP)


def t_read_receipts() -> None:
    print("[t_read_receipts]")
    eng = _Engine([(1, "c1", "sent", "say", "文件传输助手", "platform_id", "", "", "", "2026-10-21 10:00:00")])
    rows = G.read_receipts(engine=eng, days=7, now=1_000_000.0)
    check("映射成 dict", rows and rows[0]["status"] == "sent", str(rows))
    check("带时间下界", any("since" in p for p in eng.params), str(eng.params))
    check("只读 SELECT", all(s.upper().startswith("SELECT") for s in eng.seen), str(eng.seen))
    bad = _Engine(None)
    check("查不到 = None（未采集）", G.read_receipts(engine=bad) is None)
    check("查不到不抛", True)


def t_render() -> None:
    print("[t_render]")
    text = G.render("L1", (G.LEVEL_PASS, "过：7 天 / 30 条"), good_stats())
    check("有结论", "[过]" in text, text)
    check("有数字节", "数字（L1 口径" in text)
    check("有下一级", "下一级要做什么" in text)
    steps = G.render("L0", (G.LEVEL_WAIT, "等"), None)
    check("L0 也给清单", "文件传输助手" in steps, steps)
    check("L2 有说明", "抽检" in G.render("L2", (G.LEVEL_WAIT, "等"), None))


def t_fmt() -> None:
    print("[t_fmt]")
    check("None 是未采集", G._fmt(None) == "未采集")
    check("百分比", G._fmt(0.1234, "%") == "12.3%")
    check("整数去掉小数点", G._fmt(7.0) == "7")
    check("小数保留一位", G._fmt(2.55) == "2.55")


def t_readonly_discipline() -> None:
    print("[t_readonly_discipline]")
    src = (ROOT / "scripts/stage_gate.py").read_text(encoding="utf-8")
    upper = src.upper()
    for word in ("INSERT ", "UPDATE ", "DELETE "):
        check(f"源码无 {word.strip()}", word not in upper)
    check("用只读句柄", "read_only_engine" in src)
    check("复用 l0_glance 判 L0", "l0_glance.verdict" in src)
    check("未采集口径写在文档里", "未采集" in src)


def main() -> int:
    t_rate()
    t_self_rows()
    t_owner_targets()
    t_day_of()
    t_distinct_days()
    t_l1_stats_shape()
    t_l1_stats_empty()
    t_judge_pass()
    t_judge_wait()
    t_judge_fix()
    t_judge_stop()
    t_read_receipts()
    t_render()
    t_fmt()
    t_readonly_discipline()
    print()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    for f in FAILS:
        print("  -", f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
