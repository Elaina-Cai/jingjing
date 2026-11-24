# -*- coding: utf-8 -*-
"""写要原子：唯一临时名 + rename + 退避重试（BR-007）。

守一句话：**两个进程同时写同一个文件，落地的必须永远是完整的一份**。

反例（改之前真在跑的形状）：
    tmp = p.with_name(p.name + ".tmp")   # 固定名：两边写进同一个 tmp，rename 上去是两段交织的内容
    tmp.write_text(...); os.replace(tmp, p)

只有 rename 是原子的，写 tmp 不是。所以「唯一」不是洁癖，是这条路径唯一能靠的东西。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from core.infra import jsonio

ROOT = Path(__file__).resolve().parents[2]


def _sources(*roots: str) -> dict[str, str]:
    out = {}
    for root in roots:
        for path in (ROOT / root).rglob("*.py"):
            out[str(path.relative_to(ROOT)).replace(os.sep, "/")] = path.read_text(encoding="utf-8")
    return out


# ---- 结构锁：写口只有一个，别的模块不许自己拼 tmp ---------------------------------

# 允许自己 rename 的只有「轮转/归档」：rename 在那里是给旧文件改名，不是提交新内容。
# 每条都要写理由，且文件一旦不再命中就报"白名单过期"，防止名单变成免死金牌。
RENAME_ALLOWED = {
    "core/infra/log_rotate.py": "日志轮转：rename 是把旧文件改名归档，不产生新内容",
    "core/observability/decision_trace.py": "trace 轮转，同上",
    "core/execution/gui/lease.py": "lease 的临时名本来带 uuid4 唯一后缀，是对的写法",
}


def test_only_jsonio_renames_into_place() -> None:
    hits = [rel for rel, src in _sources("core", "loop", "scheduler", "agents").items()
            if rel != "core/infra/jsonio.py" and "os.replace(" in src]
    unexpected = sorted(set(hits) - set(RENAME_ALLOWED))
    assert unexpected == [], f"这些模块自己做了 rename，应该改调 jsonio.atomic_write_json：{unexpected}"
    stale = sorted(set(RENAME_ALLOWED) - set(hits))
    assert stale == [], f"白名单过期（这些文件已经不再自己 rename，删掉条目）：{stale}"


def test_no_fixed_temp_name_anywhere() -> None:
    """真正会做出坏文件的只有一种写法：临时名是固定字符串（并发两段交织）。
    唯一名用 mkstemp 或 uuid 都行，这里只禁固定名。"""
    dirs = ["core", "loop", "scheduler", "agents"]
    if (ROOT / "scripts").is_dir():
        dirs.append("scripts")
    bad = []
    for rel, src in _sources(*dirs).items():
        if rel == "core/infra/jsonio.py":
            continue
        for pat in ('+ ".tmp"', '+ ".json.tmp"', 'with_suffix(".tmp")', 'with_suffix(".json.tmp")'):
            if pat in src:
                bad.append(f"{rel} :: {pat}")
    assert bad == [], f"固定临时名（并发写会交织成坏文件）：{bad}"


def test_temp_name_is_not_a_fixed_name() -> None:
    src = (ROOT / "core/infra/jsonio.py").read_text(encoding="utf-8")
    assert "mkstemp" in src, "唯一临时名要由 mkstemp 生成"
    assert '.with_name(p.name + ".tmp")' not in src, "固定临时名一旦并发就是两段交织的内容"


# ---- 行为：临时名唯一 / fsync 在 rename 之前 / 退避重试 --------------------------

def test_each_write_uses_its_own_temp_path(tmp_path: Path, monkeypatch) -> None:
    seen: list[str] = []
    real = jsonio.os.replace

    def spy(src, dst):
        seen.append(str(src))
        return real(src, dst)

    monkeypatch.setattr(jsonio.os, "replace", spy)
    p = tmp_path / "state.json"
    assert jsonio.atomic_write_text(p, "one")
    assert jsonio.atomic_write_text(p, "two")
    assert len(seen) == 2 and seen[0] != seen[1], f"两次写用了同一个临时名：{seen}"
    assert all(Path(s).parent == tmp_path for s in seen), "临时文件要和目标同目录，跨盘 rename 不原子"
    assert p.read_text(encoding="utf-8") == "two"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["state.json"], "不许留下 .tmp 残骸"


def test_fsync_happens_before_rename(tmp_path: Path, monkeypatch) -> None:
    order: list[str] = []
    real_fsync, real_replace = jsonio.os.fsync, jsonio.os.replace
    monkeypatch.setattr(jsonio.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(jsonio.os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1])
    assert jsonio.atomic_write_text(tmp_path / "a.json", "x")
    assert order == ["fsync", "replace"], f"没先落盘就改名，断电时 rename 上去的是空文件：{order}"


def test_retries_then_succeeds(tmp_path: Path, monkeypatch) -> None:
    real = jsonio.os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("被 Defender 占着")
        return real(a, b)

    monkeypatch.setattr(jsonio.os, "replace", flaky)
    p = tmp_path / "a.json"
    assert jsonio.atomic_write_text(p, "ok") is True
    assert calls["n"] == 3
    assert p.read_text(encoding="utf-8") == "ok"


def test_gives_up_and_cleans_up(tmp_path: Path, monkeypatch) -> None:
    def denied(a, b):
        raise PermissionError("一直被占着")

    monkeypatch.setattr(jsonio.os, "replace", denied)
    p = tmp_path / "a.json"
    assert jsonio.atomic_write_text(p, "never") is False
    assert not p.exists()
    assert [x.name for x in tmp_path.iterdir()] == [], "失败也要收走自己的临时文件"


def test_concurrent_writers_never_interleave(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "state.json"
    payloads = [{"who": i, "pad": str(i) * 400} for i in range(8)]
    errs: list[BaseException] = []

    def hammer(obj):
        try:
            for _ in range(5):
                assert jsonio.atomic_write_json(p, obj, indent=1)
        except BaseException as exc:  # noqa: BLE001
            errs.append(exc)

    threads = [threading.Thread(target=hammer, args=(o,)) for o in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errs == []
    got = json.loads(p.read_text(encoding="utf-8"))
    assert got in payloads, "落地的是半份/交织的内容：说明临时名不唯一"
