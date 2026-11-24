# -*- coding: utf-8 -*-
"""整组入队 + 保序认领 + 打字节奏（2026-11-01）。

起因：`ports.say_many` 里那条"整组一次入队"的分支，把组号写成了
`_time.time()` —— 而那个函数只 `import time`、没有 `_time` 这个别名。
于是一次开口抛一次 NameError，被 `except` 静默吞掉后退化成逐条入队。
`logs/silent_warnings.log` 里躺着 136 条这条错，最早一条在 2026-10-28
（正好是这个分支上线的当天）：**组保序 / 整组 TTL / 整组成败 从未生效过**。

所以要修的不只是那个名字 —— 还得先证明这条从未跑过的路径是对的：
顺序靠 `claim_next` 的 seq 规则，节奏靠 `next_at` 按 seq 错开。

**隔离**：`db.world_db.PATH` 是写死的 `ROOT/data/world.db`，
`scripts/run_all_tests.py` 的隔离根**不覆盖**它（跑测试写生产库这个事故
这个项目已经出过两次）。所以本文件自己把库拷到临时目录再改 PATH，
生产库一行都不动。
"""
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _probe_db() -> str:
    """把 data/world.db 拷一份到临时目录，返回副本路径（生产库只读不写）。"""
    import tempfile

    d = tempfile.mkdtemp(prefix="world_probe_")
    dst = os.path.join(d, "world.db")
    for suffix in ("", "-wal", "-shm"):
        src = os.path.join(ROOT, "data", "world.db" + suffix)
        if os.path.exists(src):
            shutil.copy(src, dst + suffix)
    return dst


def _enqueue_three(stagger_s: float = 0.6):
    """在副本上入队三条，返回 (W, group_id, ids, 三个后缀文件的目录)。"""
    import db.world_db as W

    W.PATH = _probe_db()
    gid = "many:probe:%d" % int(time.time() * 1000)
    ids = W.enqueue(gid, "probe-chat",
                    [{"kind": "text", "text": "第一句"},
                     {"kind": "text", "text": "第二句"},
                     {"kind": "text", "text": "第三句"}],
                    platform="wechat", turn_id="probe-turn",
                    ttl_s=600.0, stagger_s=stagger_s)
    return W, gid, ids


def test_enqueue_keeps_seq_and_one_group():
    W, gid, ids = _enqueue_three()
    assert len(ids) == 3, ids
    con = W.conn()
    rows = [dict(r) for r in con.execute(
        "select id, group_id, seq, state from outbound_queue"
        " where group_id=? order by seq", (gid,)).fetchall()]
    con.close()
    assert [r["seq"] for r in rows] == [0, 1, 2], rows
    assert all(r["group_id"] == gid for r in rows), rows
    assert all(r["state"] == "pending" for r in rows), rows


def test_stagger_keeps_the_typing_rhythm():
    """顺序由 seq 保证，节奏得靠 next_at 错开 —— 否则 desk 一轮循环全推出去。"""
    W, gid, ids = _enqueue_three(stagger_s=0.6)
    con = W.conn()
    rows = [dict(r) for r in con.execute(
        "select seq, next_at from outbound_queue where group_id=? order by seq",
        (gid,)).fetchall()]
    con.close()
    gaps = [rows[i]["next_at"] - rows[i - 1]["next_at"] for i in (1, 2)]
    assert all(0.55 <= g <= 0.65 for g in gaps), "节奏没保住：%s" % gaps


def test_claim_blocks_until_earlier_seq_is_done():
    W, gid, ids = _enqueue_three()
    # now 推到所有 next_at 之后：这样唯一的阻挡只剩 seq 规则，
    # 免得"还没到点"和"组保序"两件事混在一起验不出来。
    future = time.time() + 5.0

    first = W.claim_next(platform="wechat", owner="probe-1", now=future)
    assert first and first["seq"] == 0, first
    assert first["id"] == ids[0]

    blocked = W.claim_next(platform="wechat", owner="probe-2", now=future)
    assert blocked is None, "前一条没走完，不许认领后面的"

    assert W.settle(ids[0], True, ack="confirmed", owner="probe-1", attempt_no=first["attempts"]) == "sent"
    second = W.claim_next(platform="wechat", owner="probe-3", now=future)
    assert second and second["seq"] == 1, second
    assert second["id"] == ids[1]


def test_say_many_group_id_has_no_ghost_name():
    """守的是"名字必须真的存在"——幽灵名字会被 except 吞成静默降级。"""
    src = open(os.path.join(ROOT, "loop", "ports.py"), encoding="utf-8").read()
    i = src.find("async def say_many(")
    assert i > 0
    body = src[i:src.find("\nasync def ", i + 20)]
    j = body.find("if not dry_run:")
    blk = body[j:body.find("sent = []", j)]
    gid_line = [ln for ln in blk.splitlines() if "_gid =" in ln]
    assert gid_line, "整组入队要生成组号"
    assert "time.time()" in gid_line[0], gid_line[0]
    assert "_time" not in gid_line[0], "本函数没有 `_time` 别名，写了就是 NameError"


def test_say_many_passes_the_rhythm_through():
    src = open(os.path.join(ROOT, "loop", "ports.py"), encoding="utf-8").read()
    i = src.find("async def say_many(")
    body = src[i:src.find("\nasync def ", i + 20)]
    assert "stagger_s=_gap_s" in body, "整组入队要把节奏传下去"
    assert "_gap_s = random.uniform(*gap)" in body


def main() -> bool:
    names = sorted(n for n in globals() if n.startswith("test_"))
    fails = []
    for n in names:
        fn = globals()[n]
        if not callable(fn):
            continue
        try:
            fn()
            print("  ok   %s" % n)
        except Exception as exc:  # noqa: BLE001 - 汇总用，单个用例炸了要继续跑完
            fails.append(n)
            print("  FAIL %s  %s" % (n, repr(exc)[:200]))
    print("RESULT %d failed / %d" % (len(fails), len(names)))
    return not fails


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
