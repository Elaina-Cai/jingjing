"""PIT-044：要“随机”的地方用键控抽样，同一个输入给同一个答案。

反例形状（以前会出的）：
- ``if random.random() > keep: skip``：日志里没有种子，线上那次触发无法还原；
- ``abs(hash(item_id))``：``PYTHONHASHSEED`` 一变结果就换一套，“复现”失效。
"""

import glob
import os
import re
import subprocess
import sys

from core.infra import det

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME_ROOTS = ("core", "loop", "scheduler", "agents")
# 这些位置的随机是“人类手感”或“抖动”，不参与判定，也不影响可复现的结论：
ALLOW_RANDOM = {
    os.path.join("core", "execution", "wx_human.py"),  # 打字速度、错字节奏
    os.path.join("loop", "ports.py"),                  # 分段发送的停顿抖动
    os.path.join("core", "infra", "det.py"),           # 本模块自己就是随机源
}
PROBE = (
    "import sys;sys.path.insert(0,'.');"
    "from core.infra import det;"
    "print(det.seed_of('proactive','c1','202610291100'));"
    "print(int(det.chance(0.37,'proactive','c1','202610291100')));"
    "print(det.rng('noise','item-7').random())"
)


def _sources():
    for root in RUNTIME_ROOTS:
        pattern = os.path.join(ROOT, root, "**", "*.py")
        for path in glob.glob(pattern, recursive=True):
            rel = os.path.relpath(path, ROOT)
            if "__pycache__" in rel:
                continue
            yield rel, path


def test_same_key_same_answer():
    a = [det.chance(0.5, "proactive", "chat-1", "202610291100") for _ in range(20)]
    assert len(set(a)) == 1
    assert a[0] == det.chance(0.5, "proactive", "chat-1", "202610291100")


def test_different_key_moves_the_draw():
    draws = {det.chance(0.5, "proactive", "chat-%d" % i) for i in range(200)}
    assert draws == {True, False}


def test_chance_boundaries():
    assert det.chance(0.0, "x") is False
    assert det.chance(-1.0, "x") is False
    assert det.chance(1.0, "x") is True
    assert det.chance(3.0, "x") is True


def test_rate_is_roughly_p():
    hits = sum(1 for i in range(1000) if det.chance(0.3, "rate", i))
    assert 200 <= hits <= 400, hits


def test_parts_do_not_collide():
    assert det.seed_of("ab", "c") != det.seed_of("a", "bc")
    assert det.seed_of(None) == det.seed_of("")


def test_stable_across_processes_and_hash_seeds():
    outs = []
    for hashseed in ("0", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=hashseed)
        proc = subprocess.run(
            [sys.executable, "-c", PROBE],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-800:]
        outs.append(proc.stdout.strip())
    assert outs[0] == outs[1], outs


def test_no_bare_random_in_judgement_paths():
    offenders = []
    for rel, path in _sources():
        if os.path.normpath(rel) in ALLOW_RANDOM:
            continue
        text = open(path, encoding="utf-8").read()
        # 只看「用了 stdlib random」：``det.rng(...).random()`` 是键控抽样，允许。
        if re.search(r"^\s*(import random\b|from random import\b)", text, re.M):
            offenders.append(rel)
    assert offenders == [], offenders


def test_bench_replay_does_not_seed_from_hash():
    path = os.path.join(ROOT, "scripts", "bench_replay.py")
    text = open(path, encoding="utf-8").read()
    assert re.search(r"\bhash\(", text) is None
    assert "det.rng(" in text
