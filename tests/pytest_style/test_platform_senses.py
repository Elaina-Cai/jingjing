"""她「看得见什么、动得了什么手」的纪律锁。

为什么要有这把锁
----------------
平台接线最容易出的事不是接错，而是**没人知道接没接**：
面板一个绿灯，她其实既看不见也动不了。所以声明表必须显式（不许靠默认值蒙混），
判据必须通用（不许 `if platform == "wechat"`），读不出来必须显示成「看不出来」。
"""

from __future__ import annotations

import ast
import json
import os
import time
from pathlib import Path

import pytest

from core.execution.platform_registry import (
    PlatformDef,
    Sense,
    all_platforms,
    get_platform,
)
from core.infra import senses

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "core" / "execution" / "platform_registry.py"
SENSES_PY = ROOT / "core" / "infra" / "senses.py"

WIRED = {"wechat", "qq"}
UNWIRED = {"telegram", "discord", "bilibili", "douyin", "dingtalk", "feishu", "web"}


def _platform_def_keywords() -> list[set[str]]:
    tree = ast.parse(REGISTRY.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "register_platform":
            for arg in node.args:
                if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "PlatformDef":
                    out.append({kw.arg for kw in arg.keywords if kw.arg})
    return out


def test_every_platform_declares_senses_explicitly():
    """每个平台都要亲手写 senses=（可以为空），不许靠默认值 —— 默认值会让「没接」看起来像「没写」。"""
    blocks = _platform_def_keywords()
    assert len(blocks) >= 9, blocks
    missing = [i for i, kws in enumerate(blocks) if "senses" not in kws]
    assert not missing, f"这些 PlatformDef 没写 senses=（第 {missing} 个注册）"


def test_unwired_platforms_promise_nothing():
    for key in UNWIRED:
        p = get_platform(key)
        assert p.senses == (), f"{key} 还没接，不该先许诺能力：{[s.key for s in p.senses]}"


def test_wired_platforms_have_both_axes():
    for key in WIRED:
        p = get_platform(key)
        axes = {s.axis for s in p.senses}
        assert axes == {"perceive", "act"}, f"{key} 缺一条轴：{axes}"


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(key="x", axis="talk", says="会说话", proof="ack"),
        dict(key="x", axis="perceive", says="能看见", proof="beat"),            # 缺 source/beat_ms
        dict(key="x", axis="perceive", says="能看见", proof="beat", source="decrypted"),
        dict(key="x", axis="act", says="能动手", proof="ack", beat_ms=1000),     # 操作不许有时效
        dict(key="x", axis="act", says="send_text", proof="ack"),               # 人话里不许有下划线
        dict(key="x", axis="act", says="", proof="ack"),
    ],
)
def test_sense_rejects_bad_combinations(kwargs):
    with pytest.raises(ValueError):
        Sense(**kwargs)


def _write_beat(name: str, *, age_ms: int = 0, beat_ms: int = 60_000) -> None:
    """按 liveness 的契约手写一份心跳（不经过 provider，测的是读法）。"""
    from core.infra import liveness

    path = liveness.path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"pid": os.getpid(), "at_ms": int(time.time() * 1000) - age_ms, "beat_ms": beat_ms}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def test_evidence_is_never_ok_without_a_heartbeat(monkeypatch, tmp_path):
    """没有心跳 = 这条通道没在听。不许是 ok，也不许是「不知道」。"""
    from core.infra import liveness

    monkeypatch.setattr(liveness, "LOGS_DIR", str(tmp_path))
    qq = get_platform("qq")
    per = [s for s in qq.senses if s.axis == "perceive"]
    assert per
    assert all(senses.evidence(s) != "ok" for s in per)
    assert all(senses.evidence(s) == "stale" for s in per), "没心跳应该是「有点旧/没在听」"


def test_fresh_heartbeat_makes_it_ok_and_old_one_does_not(monkeypatch, tmp_path):
    from core.infra import liveness

    monkeypatch.setattr(liveness, "LOGS_DIR", str(tmp_path))
    per = [s for s in get_platform("qq").senses if s.axis == "perceive"]

    _write_beat("qq")
    assert all(senses.evidence(s) == "ok" for s in per)
    row = senses.row(get_platform("qq"))
    assert row["evidence"] == "ok" and "刚看过" in row["line"]

    _write_beat("qq", age_ms=10 * 60_000)
    assert all(senses.evidence(s) == "stale" for s in per)


def test_checkup_never_reports_ok_without_evidence(monkeypatch, tmp_path):
    from core.infra import liveness
    from console import checkup

    monkeypatch.setattr(liveness, "LOGS_DIR", str(tmp_path))
    assert checkup.check_senses()["level"] != "ok"


def test_qq_beat_uses_the_shared_clock():
    """QQ 的「我还在听」必须走 liveness，且心跳文件名不许出现在 provider 里。"""
    text = (ROOT / "core" / "execution" / "qq_provider.py").read_text(encoding="utf-8")
    assert 'liveness.path("qq")' in text
    assert "qq_link.json" not in text, "心跳文件名只能出现在 core/infra/liveness.py"
    assert "liveness.age_ms(" in SENSES_PY.read_text(encoding="utf-8")


def test_row_shapes_are_the_four_states():
    see = Sense("read_chat", "perceive", "能看见聊天内容", "beat", "decrypted", 60_000)
    act = Sense("send_text", "act", "能打字回人", "ack")

    def row(*senses_):
        return senses.row(PlatformDef(key="t", display="试", source="t", senses=senses_), now=1_000_000.0)

    assert row()["shape"] == "none"
    assert row(see, act)["shape"] == "both"
    assert row(see)["shape"] == "see_only"
    blind = row(act)
    assert blind["shape"] == "act_only"
    assert "看不见" in blind["line"]


def test_judgement_never_branches_on_platform_name():
    """判据只按 proof 走；出现按平台名分支就说明又在给每个平台写一份。"""
    tree = ast.parse(SENSES_PY.read_text(encoding="utf-8"))
    names = set(all_platforms())
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for side in [node.left, *node.comparators]:
                if isinstance(side, ast.Constant) and isinstance(side.value, str) and side.value in names:
                    bad.append((node.lineno, side.value))
    assert not bad, f"core/infra/senses.py 里出现按平台名分支：{bad}"
