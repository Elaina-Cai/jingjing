# -*- coding: utf-8 -*-
"""第 4 批：失败语义三态 + jsonl 有界读（2026-11-01）。

守四件事：
1. `collector._append_history` 不许再整读 history.jsonl（曾每 2 秒全量读 7MB、
   只用最后 240 行），只许读尾部；
2. history.jsonl 要有轮转策略（此前零策略，~2.6MB/天无限涨）；
3. `brain.remember` 空文本/写失败要如实说（曾对空文本也回「记到常驻板上了」
   + result="ok"，而 memo.add 对空文本什么都不写）；
4. `diagnose_dump` 的日志尾巴不许整读 20MB 的 yinyue.log。
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_append_history_only_reads_tail():
    """3000 行历史 + 新 1 条 → 返回 ≤240 个点，且最后一个是刚写的那条。"""
    import console.collector as C

    d = tempfile.mkdtemp(prefix="hist_probe_")
    tmp = os.path.join(d, "history.jsonl")
    with open(tmp, "w", encoding="utf-8") as f:
        for i in range(3000):
            f.write(json.dumps({"at": "2026-11-01 00:%02d:%02d" % (i // 60 % 60, i % 60),
                                "social": 0.1, "arousal": 0.2}) + "\n")

    old = C.HISTORY
    C.HISTORY = tmp
    try:
        pts = C._append_history({"social": 0.9, "arousal": 0.8})
    finally:
        C.HISTORY = old

    assert 1 <= len(pts) <= 240, len(pts)
    last = pts[-1]
    assert last.get("social") == 0.9 and last.get("arousal") == 0.8, last
    with open(tmp, encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert len(lines) == 3001, "要恰好追加一条，不能重写历史"
    assert json.loads(lines[-1])["social"] == 0.9


def test_history_rotation_policy_declared():
    """history.jsonl 要在 DEFAULT_POLICIES 里有名字 —— 没名字 = 永远无限涨。"""
    from core.infra.log_rotate import policy_for

    pol = policy_for("history.jsonl")
    assert pol.max_bytes >= 1 * 1024 * 1024, pol
    assert 1 <= pol.backups <= 10, pol


def test_rotate_if_needed_renames_and_appender_recovers():
    """用一个小策略实跑轮转：切完之后 append 还能正常重建文件。"""
    from core.infra.log_rotate import Policy, rotate_if_needed

    d = tempfile.mkdtemp(prefix="rot_probe_")
    p = os.path.join(d, "history.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write("x" * 2048)
    got = rotate_if_needed(p, policy=Policy(max_bytes=1024, backups=2))
    assert got == "rename", got
    assert not os.path.exists(p), "切分后主文件应已挪成 .1，等下一次 append 重建"
    assert os.path.exists(p + ".1") and os.path.getsize(p + ".1") == 2048
    # 再到上限 → 顺移成 .2，.1 是新的
    with open(p, "w", encoding="utf-8") as f:
        f.write("y" * 2048)
    got2 = rotate_if_needed(p, policy=Policy(max_bytes=1024, backups=2))
    assert got2 == "rename", got2
    assert os.path.exists(p + ".2") and os.path.getsize(p + ".2") == 2048


def test_remember_is_three_state():
    """空文本 → empty；写失败 → failed；只有真写进去才 ok。

    _exec 的上下文太重，不好单测，这里按本仓库惯例做源码级锁：
    空文本判断必须出现在 memo.add **之前**，failed 分支必须存在。
    """
    src = open(os.path.join(ROOT, "loop", "brain.py"), encoding="utf-8").read()
    i = src.find('if name == "remember":')
    assert i > 0
    body = src[i:src.find('if name == "remember"', i + 10) if src.find('if name == "remember"', i + 10) > 0 else len(src)]
    body = body[:body.find("\n    receipt = ledger.append_receipt(act=\"error\"")]
    j_empty = body.find('if not _txt.strip():')
    j_add = body.find("MemoryService().remember(")
    assert 0 < j_empty < j_add, "空文本判断要在真正写记忆之前"
    assert 'result="empty"' in body and 'result="failed"' in body, body[:600]
    # 2026-11-21：remember 已从常驻板 memo.add 换成长期记忆服务，写进去才回 ok
    assert 'result="ok"' in body and "_item is None" in body, "只有真写进去才算 ok"


def test_myself_receipt_is_not_ok_when_nothing_written():
    """四个字段全空时回执不许说 ok（原来叫 "noted"，跟正文「没写」自相矛盾）。"""
    src = open(os.path.join(ROOT, "loop", "brain.py"), encoding="utf-8").read()
    assert '"ok" if _out else "empty"' in src, "myself 的空回调执要记 empty"
    assert 'result="ok" if _out else "noted"' not in src


def test_diagnose_log_tail_is_bounded():
    """20MB 的 yinyue.log 不许为 200 行尾巴整读。"""
    src = open(os.path.join(ROOT, "console", "executor.py"), encoding="utf-8").read()
    i = src.find("def diagnose_dump(")
    assert i > 0
    blk = src[i:src.find("\ndef _stop_files", i)]
    j = blk.find('"log_tail"')
    seg = blk[max(0, j - 800):j + 200]
    assert 'fh.seek(' in seg, "要 seek 到尾部再读"
    assert '.read().splitlines()' not in seg, "不许整读"


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
