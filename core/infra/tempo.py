# -*- coding: utf-8 -*-
"""她的「人味节奏」——唯一一张表（UI-066 乙，2026-10-29）。

这里只放**为了像人而定**的阈值：多久算冷场、多久内算「刚开过口」、同一句复读多久不
算新信息、追同一件事隔多久。它们不是物理常量，没有对错，只有像不像。

不放在这里的，以及边界在哪里：
* 机器时间（超时/轮询/等界面/进程存活）在 `core/runtime_config.py::DEFAULTS`；
* 平台硬规则（微信发出后 2 分钟内才能撤回）留在原模块 —— 那是被别人定死的，
  不是她的性格；把它搬进来会让人误以为「调大一点就能撤更久」。

单位一律毫秒（与 `core/infra/duration.py` 同一刻度）。换算成秒/分钟/小时只在本模块
做，别处不许再 `* 60` 或 `/ 1000` —— 踩坑 #15 就是两个「10 分钟」长得一样撞出来的。

覆盖：`configs/runtime.json` 里写

    {"tempo": {"voice.window": 900000}}

没写就是 `DEFAULTS`。写坏了（类型不对/负数）回落默认并在 stderr 报一行，不抛：
她的节奏不该因为一句配置写错就整个停摆。
"""
from __future__ import annotations

from safelog import warn

from core import runtime_config as rt
from core.infra import duration

#: 键 -> (默认毫秒, 语义)。加键必须同时给语义，否则 `table()` 会把它露出来。
DEFAULTS: dict = {
    "voice.window": (600_000, "主人十分钟内开过口，才许在群里换成语音（全库唯一口径）"),
    "noise.repeat": (600_000, "十分钟内同一人复读同一句，不算新信息"),
    # 2026-11-02 删掉 "loops.cooldown" 与 "verify.batch_cooldown"：它们的消费者
    # （core/context/open_loops.py、core/context/guess_verify.py、core/execution/owner_ask.py）
    # 已随旧引擎一起删除，全库再无一处读它们 —— 留着就是两个「假的可调」，
    # 而表自己的规矩写着「每个旋钮都要有消费者」。要重新做那两件事时再加回来。
    "brain.same_target": (0, "同一对象连发冷却（0 = 关：间隔只问 ledger.quiet_ms）；只管跨回合"),
    "brain.bubble_gap": (1_500, "同一回合里连发第二条气泡前的最小间隔（她说的是「停 1.5 秒」）"),
    "ledger.blocked": (1_800_000, "冷却判定窗口：半小时内她说过话的目标算「热」"),
    "ledger.facts": (1_800_000, "「最近说过什么」的事实窗口：半小时"),
    "ledger.recent": (3_600_000, "话头窗口：一小时内接过话的目标"),
    "admission.exchange": (1_800_000, "半小时内的一来一回算「一段对话」"),
}

_UNITS = ("ms", "s", "minutes", "hours")


def _names() -> str:
    return ", ".join(sorted(DEFAULTS))


def _overrides() -> dict:
    """configs/runtime.json 的 "tempo" 子树；没有/写坏给空 dict。"""
    try:
        got = rt.section("tempo")
    except Exception as exc:  # noqa: BLE001 - 配置读不到不是致命错
        warn("tempo_section_failed", err=repr(exc))
        return {}
    return got if isinstance(got, dict) else {}


def _raw(name: str) -> int:
    """键 -> 毫秒整数。未知名抛 KeyError（拼错的旋钮不许静默），坏值回落默认。"""
    if name not in DEFAULTS:
        raise KeyError("tempo 表里没有 %r；可用：%s" % (name, _names()))
    default = int(DEFAULTS[name][0])
    got = _overrides().get(name)
    if got is None:
        return default
    if isinstance(got, bool) or not isinstance(got, int):
        warn("tempo_override_not_int",
             key=name, got=repr(got), default_ms=default)
        return default
    if got < 0:
        warn("tempo_override_negative",
             key=name, got=got, default_ms=default)
        return default
    return int(got)


def ms(name: str) -> int:
    """旋钮的毫秒值。"""
    return _raw(name)


def s(name: str) -> float:
    """旋钮的秒值（给还在用秒判定的老路径）。"""
    return _raw(name) / 1000.0


def minutes(name: str) -> int:
    """旋钮的整分钟数（给 `window_min` 这类签名用）。"""
    return _raw(name) // 60_000


def hours(name: str) -> float:
    """旋钮的小时数。"""
    return _raw(name) / 3_600_000.0


def table() -> list:
    """一张给人看的表：键、当前值、默认值、是否被改过、语义。体检与控制台都读它。"""
    rows = []
    for name in sorted(DEFAULTS):
        default, why = DEFAULTS[name]
        value = _raw(name)
        try:
            shown = "已关闭" if value == 0 else duration.human(value)
        except Exception:  # noqa: BLE001 - 渲染失败不影响判定
            shown = "%d ms" % value
        rows.append({"key": name, "ms": value, "default_ms": int(default),
                     "overridden": value != int(default), "human": shown, "why": why})
    return rows
