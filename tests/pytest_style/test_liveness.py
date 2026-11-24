"""心跳只有一份契约：at(epoch) + beat_s(承诺节拍) + pid。

2026-10-28 的事故形状：引擎写 `loop_engine.json`，监管器读 `loop_heartbeat.json`，
判死靠写死 120 秒 —— 引擎卡住时监管器一切正常。这个文件钉住三件事：
1. 判死只看 `now - at > beat_s * GRACE`，不认写死秒数；
2. 生产者没声明 beat_s 就判不出来（不许猜默认值）；
3. 心跳文件名只许出现在 core/infra/liveness.py。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.infra import liveness  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def hb(tmp_path, monkeypatch):
    monkeypatch.setattr(liveness, "LOGS_DIR", str(tmp_path))

    def write(name, obj):
        with open(liveness.path(name), "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
    return write


def _at_ms(ago_ms: int = 0) -> int:
    return int((time.time() - ago_ms / 1000.0) * 1000)


def test_fresh_heartbeat_is_alive(hb):
    hb("engine", {"pid": os.getpid(), "at_ms": _at_ms(), "beat_ms": 10_000})
    assert liveness.alive("engine") is True


def test_stale_heartbeat_is_dead(hb):
    hb("engine", {"pid": os.getpid(), "at_ms": _at_ms(100_000), "beat_ms": 10_000})
    assert liveness.alive("engine") is False
    assert liveness.state("engine") == "stale"


def test_deadline_is_declared_by_producer_not_hardcoded(hb):
    # 容忍窗 = 生产者承诺的 beat_ms × GRACE，不是谁写死的秒数
    hb("engine", {"pid": os.getpid(), "at_ms": _at_ms(20_000), "beat_ms": 10_000})
    assert liveness.deadline_ms('engine') == 30_000
    assert liveness.alive("engine") is True
    hb("engine", {"pid": os.getpid(), "at_ms": _at_ms(20_000), "beat_ms": 5_000})
    assert liveness.deadline_ms('engine') == 15_000
    assert liveness.alive("engine") is False


def test_missing_beat_is_undecidable_not_default(hb):
    hb("engine", {"pid": os.getpid(), "at_ms": _at_ms()})
    assert liveness.deadline_ms('engine') is None
    assert liveness.alive("engine") is False
    assert "beat_ms" in liveness.why("engine")


def test_fresh_but_pid_gone_is_dead(hb):
    hb("engine", {"pid": 999999, "at_ms": _at_ms(), "beat_ms": 10_000})
    assert liveness.alive("engine") is False
    assert "pid" in liveness.why("engine")


def test_unknown_name_raises(hb):
    with pytest.raises(KeyError):
        liveness.path("nope")


def test_no_heartbeat_filenames_outside_liveness():
    """心跳文件名是单点：别处再写一遍，就会长回“两个文件”的形状。"""
    bad = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", ".test_tmp", "data", "logs", "output",
                                                "__pycache__", ".venv", ".venv_it", ".venv_iso", ".venv_tts", "backups", "_scratch")]
        for f in files:
            if not f.endswith(".py"):
                continue
            fp = os.path.join(root, f)
            if os.path.normpath(fp).endswith(os.path.join("core", "infra", "liveness.py")):
                continue
            if os.sep + "tests" + os.sep in fp:
                continue
            if os.path.basename(fp).startswith("__Users__"):
                continue  # 早年路径转义写坏留下的副本，见踩坑档案
            try:
                src = open(fp, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if "loop_engine.json" in src or "loop_heartbeat.json" in src:
                bad.append(os.path.relpath(fp, ROOT))
    assert not bad, "这些文件里还写着心跳文件名：%r" % (bad,)


def test_supervisor_has_no_hardcoded_timeout():
    src = open(os.path.join(ROOT, "scripts", "supervisor.py"), encoding="utf-8").read()
    assert "liveness.alive(" in src
    assert "max_age_s" not in src

# ---------- 四态：unknown 不是 dead（2026-10-28 升级期踩到的那一下） ----------

def test_tristate(tmp_path, monkeypatch):
    import json as _json
    import time as _time

    from core.infra import liveness as L

    p = tmp_path / "h.json"
    monkeypatch.setattr(L, "path", lambda name: str(p))
    assert L.state("engine") == "gone"          # 没文件

    p.write_text(_json.dumps({"pid": os.getpid(), "at_ms": int(_time.time() * 1000)}), encoding="utf-8")
    assert L.state("engine") == "unknown"       # 有 at 没 beat_s = 不可判
    assert L.alive("engine") is False
    assert any("beat_ms" in x for x in L.contract("engine"))

    p.write_text(_json.dumps({"pid": os.getpid(), "at_ms": int(_time.time() * 1000), "beat_ms": 10_000}),
                 encoding="utf-8")
    assert L.state("engine") == "alive"

    p.write_text(_json.dumps({"pid": os.getpid(), "at_ms": int(_time.time() * 1000) - 3_600_000, "beat_ms": 10_000}),
                 encoding="utf-8")
    assert L.state("engine") == "stale"         # 超期 = 卡死

    p.write_text(_json.dumps({"pid": 999999, "at_ms": int(_time.time() * 1000), "beat_ms": 10_000}),
                 encoding="utf-8")
    assert L.state("engine") == "gone"          # pid 不在


def test_world_health_undecidable_is_not_dead(tmp_path, monkeypatch):
    """心跳不合契约时，world_health 既不说“世界停了”也不说“没事”。"""
    import json as _json
    import time as _time

    from core.infra import liveness as L
    from loop import ports

    p = tmp_path / "h.json"
    p.write_text(_json.dumps({"pid": os.getpid(), "at_ms": int(_time.time() * 1000)}), encoding="utf-8")
    monkeypatch.setattr(L, "path", lambda name: str(p))
    got = ports.world_health()
    assert got["state"] == "unknown"
    assert got["alive"] is False
    assert "契约" in got["note"]


def test_supervisor_uses_tristate_not_format_sniffing():
    """监管器不许再靠“at 是不是字符串”猜新旧格式（那正是升级期误判的来源）。"""
    src = open(os.path.join(ROOT, "scripts", "supervisor.py"), encoding="utf-8").read()
    assert 'state("engine")' in src
    assert "旧格式" not in src
