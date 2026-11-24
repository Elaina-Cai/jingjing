# -*- coding: utf-8 -*-
"""UI-066 乙：她的「人味节奏」只许有一张表。

为什么要有这些锁：摸底时全库时间常量 122 个，其中 104 个长得像「人味刻度」
（分钟/小时），97 个没有任何测试碰过；另有几个被别处的结构锁按字面量钉死 —— 谁想调
她，得先去改一个不相干的测试。这张表 + 下面几条锁，让「调她」变成改配置，且改了有人
告诉你是哪一项。

锁四件事：
1. 表本身合法（键有命名空间、值是毫秒、每条有语义）；
2. 配置能覆盖、写坏了回落默认、拼错了抛 KeyError（不许静默）；
3. 每个旋钮都有消费者（死旋钮 = 假的可调）；
4. 每个旋钮都被测试碰过（下一个想调它的人能看懂边界在哪）。
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from core import runtime_config as rt
from core.context import noise_gate
from core.infra import tempo

ROOT = Path(__file__).resolve().parents[2]
SRC_DIRS = ("core", "loop", "agents", "console", "scheduler", "db")

#: 从字面量迁进表的常量：文件、现在的写法、以及「原来那句字面量」的形态。
MIGRATED = [
    ("core/rules/voice_gate.py", r"^WINDOW_S = tempo\.s\(", r"^WINDOW_S = 600"),
    ("core/context/noise_gate.py", r"^REPEAT_WINDOW_SECONDS = tempo\.s\(", r"^REPEAT_WINDOW_SECONDS = 600"),
    ("loop/brain.py", r"^SAME_TARGET_COOLDOWN_MS = tempo\.ms\(", r"^SAME_TARGET_COOLDOWN_MS = 0\b"),
    ("core/context/admission.py", r"^EXCHANGE_WINDOW_SECONDS = tempo\.s\(", r"^EXCHANGE_WINDOW_SECONDS = 1800"),
    ("loop/ledger.py", r"window_min: int = tempo\.minutes\(", r"window_min: int = \d+"),
]


def _files(dirs=SRC_DIRS):
    for name in dirs:
        for path in sorted((ROOT / name).rglob("*.py")):
            if "__pycache__" in path.as_posix():
                continue
            yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8", errors="replace")


def _test_files():
    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8", errors="replace")


def _fake_config(monkeypatch, body):
    monkeypatch.setattr(rt, "_file", lambda: body)


def test_table_is_well_formed():
    assert tempo.DEFAULTS
    for name, (ms, why) in tempo.DEFAULTS.items():
        assert re.fullmatch(r"[a-z]+(\.[a-z_]+)+", name), name
        assert isinstance(ms, int) and not isinstance(ms, bool), name
        assert ms >= 0, name
        assert len(why) >= 6, name


def test_default_without_override(monkeypatch):
    _fake_config(monkeypatch, {})
    for name, (ms, _why) in tempo.DEFAULTS.items():
        assert tempo.ms(name) == ms, name


def test_override_from_config(monkeypatch):
    _fake_config(monkeypatch, {"tempo": {"voice.window": 900_000}})
    assert tempo.ms("voice.window") == 900_000
    assert tempo.s("voice.window") == 900.0


def test_bad_override_falls_back(monkeypatch):
    for bad in ("9m", True, -5, 1.5, None):
        _fake_config(monkeypatch, {"tempo": {"voice.window": bad}})
        assert tempo.ms("voice.window") == 600_000, bad


def test_unknown_name_is_loud():
    with pytest.raises(KeyError) as err:
        tempo.ms("voice.windw")
    assert "voice.windw" in str(err.value)


def test_every_knob_has_a_consumer():
    used = set()
    for _rel, src in _files():
        used.update(re.findall(r'tempo\.(?:ms|s|minutes|hours)\("([^"]+)"\)', src))
    dead = sorted(set(tempo.DEFAULTS) - used)
    assert dead == [], dead


def test_every_knob_is_touched_by_a_test():
    text = "\n".join(src for _rel, src in _test_files())
    cold = sorted(n for n in tempo.DEFAULTS if n not in text)
    assert cold == [], cold


def test_numbers_live_only_in_the_table():
    for rel, present, gone in MIGRATED:
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert re.search(present, src, re.M), (rel, present)
        assert re.search(gone, src, re.M) is None, (rel, gone)


def test_ledger_windows_come_from_tempo():
    from loop import ledger

    assert ledger.blocked_targets.__defaults__[0] == tempo.minutes("ledger.blocked")
    assert ledger.speaking_facts.__kwdefaults__["window_min"] == tempo.minutes("ledger.facts")
    assert ledger.recent_say_targets.__defaults__[0] == tempo.minutes("ledger.recent")


def test_noise_repeat_boundary(monkeypatch):
    """「复读不算新信息」的窗口边界：正好等于窗口算复读，多一秒算新话。"""
    monkeypatch.setattr(noise_gate, "noise_gate_mode", lambda: "on")
    now = 1_800_000_000.0
    line = "我们今天还是去吃那家面吧"
    win = noise_gate.REPEAT_WINDOW_SECONDS

    noise_gate.clear_noise_state()
    first = noise_gate.noise_verdict(chat_id="c1", sender_id="s1", content=line, now=now)
    assert first.skip is False
    inside = noise_gate.noise_verdict(chat_id="c1", sender_id="s1", content=line, now=now + win)
    assert inside.skip is True

    noise_gate.clear_noise_state()
    noise_gate.noise_verdict(chat_id="c2", sender_id="s1", content=line, now=now)
    after = noise_gate.noise_verdict(chat_id="c2", sender_id="s1", content=line, now=now + win + 1)
    assert after.skip is False

#: 9 个旋钮各自落在哪个模块的哪个常量上 —— 值必须等于表里的值，不许留旧副本。
CONSUMERS = [
    ("voice.window", "core.rules.voice_gate", "WINDOW_S", "s"),
    ("noise.repeat", "core.context.noise_gate", "REPEAT_WINDOW_SECONDS", "s"),
    ("admission.exchange", "core.context.admission", "EXCHANGE_WINDOW_SECONDS", "s"),
    ("brain.same_target", "loop.brain", "SAME_TARGET_COOLDOWN_MS", "ms"),
    ("brain.bubble_gap", "loop.brain", "BUBBLE_GAP_MS", "ms"),
]


@pytest.mark.parametrize("name, mod_name, attr, unit", CONSUMERS)
def test_constant_equals_table_value(name, mod_name, attr, unit):
    """常量不是「另一个数」，而是表里的数（改配置就该跟着变）。"""
    mod = importlib.import_module(mod_name)

    assert getattr(mod, attr) == pytest.approx(getattr(tempo, unit)(name))


def test_minutes_conversion_keeps_whole_minutes():
    """分钟是取整刻度：整分钟必须精确，不整的只许向下取 —— 不许悄悄进位到 2 分钟。"""
    for name in tempo.DEFAULTS:
        ms = tempo.ms(name)
        assert tempo.minutes(name) * 60_000 <= ms, name
        if ms % 60_000 == 0:
            assert tempo.minutes(name) * 60_000 == ms, name


def test_voice_window_boundary():
    """语音触发的窗口边界：正好等于窗口算「刚开过口」，多一秒算过期。"""
    from core.rules import voice_gate as vg

    kind = sorted(vg.GROUP_KINDS)[0]
    now = 1_800_000_000.0
    win = vg.WINDOW_S

    ok, why = vg.allow(chat_kind=kind, who="member", owner_spoke_at=now - win, now=now,
                       owner_key="msg1")
    assert (ok, why) == (True, "group_owner_recent")
    late, why_late = vg.allow(chat_kind=kind, who="member", owner_spoke_at=now - win - 1,
                              now=now, owner_key="msg1")
    assert late is False
    assert why_late == "owner_stale"

# 数值快照：上面那些锁都是「相对」的（窗口改了，边界测试跟着改），所以谁把 10 分钟
# 悄悄换成 5 分钟，测试不会红 —— 而那是她性格的一部分，不是重构。这一条把「改她」变成
# 必须先动这里的一行，并留下这句提示语。
_SNAPSHOT = {
    "voice.window": 600_000,          # 群里主人开口后多久内允许她出声
    "noise.repeat": 600_000,          # 同一件事多久内不重复接话
    # loops.cooldown / verify.batch_cooldown 于 2026-11-02 删除：消费者随旧引擎一起没了
    "brain.same_target": 0,           # 0 = 不做同目标冷却（只管跨回合）
    "brain.bubble_gap": 1_500,        # 同一回合里第二条气泡的最小间隔（2026-11-02）
    "ledger.blocked": 1_800_000,      # 冷场/被堵判定窗
    "ledger.facts": 1_800_000,        # 说话事实窗
    "ledger.recent": 3_600_000,       # 最近说过什么窗
    "admission.exchange": 1_800_000,  # 一问一答的容错窗
}


def test_defaults_are_a_declared_intent():
    """改默认值 = 改她的节奏：可以，但必须动这一行，别在实现里悄悄改。"""
    now = {k: v[0] for k, v in tempo.DEFAULTS.items()}
    assert now == _SNAPSHOT, "默认节奏变了；若是有意的，同步改 _SNAPSHOT 并在交接里记一笔"
