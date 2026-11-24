"""驱动器：真实约束 → 归一化状态量（试验 · 写死版）。

纪律（施工单 §1，违反即试验失败）：
  * driver 只能读**真实量**（账本 / 时钟 / 失败计数）；
  * **禁止**手填常量、禁止读 prompt 文案、禁止写台词。

试验：写死版。抽象条件见施工单 §6。
"""
from __future__ import annotations

import time
from datetime import datetime
from safelog import warn
from core.infra.envflags import text


def today_key(now: float | None = None) -> str:
    """本地日期键（用于日切）。"""
    return datetime.fromtimestamp(now if now is not None else time.time()).strftime("%Y-%m-%d")


#: 当日消费起点（写死版内部状态；抽象后并入 StateStore 的演化规则）
_DAY_BASELINE: dict[str, object] = {"day": "", "used": 0}

#: 归一化参考量（**不是真实预算闸门**，只把"今天烧了多少"折算成 0~1）。
#: 可被 ``YINYUE_STATE_BUDGET_REF`` 覆盖。
_REF_DEFAULT = 500_000


def budget_pressure(now: float | None = None) -> float:
    """今日 token 消耗 / 日预算，clamp 到 0~1。纯算术，不可伪造。

    数据源（**已有，勿新造**）：
      * ``LLMClient._budget_used``   —— ``_record_usage`` 逐次累加
      * ``LLMClient._daily_budget``  —— ``= config.llm.daily_token_budget``

    语义修正：预算是"**日**"预算，而 ``_budget_used`` 是进程生命周期累计，
    所以本函数自己维护"当日消费起点"（跨天把当时的累计记为起点），
    得到**今日增量**。这属于状态演化，不是新造数据源。

    分母（归一化基准）：① 设了真实日预算就用它；② 否则用 ``YINYUE_STATE_BUDGET_REF``
    （默认 500k）——**只做归一化，不参与真实预算闸门**。
    读不到 ⇒ ``0.0``（不施加压力；状态链路坏了绝不拖累主链）。
    """
    try:
        import os

        from core.bootstrap import get_llm

        client = get_llm()
        used = int(getattr(client, "_budget_used", 0) or 0)
        limit = int(getattr(client, "_daily_budget", 0) or 0)
        # 2026-10-11 修正：生产 DAILY_TOKEN_BUDGET=0（unlimited）时，原来
        # "除以 _daily_budget" 恒得 0 ⇒ 整个试验不触发。改为：
        # 有真实日预算就用它；否则用**只做归一化**的参考量（不参与真实闸门）。
        ref = int(text("YINYUE_STATE_BUDGET_REF") or 0) or _REF_DEFAULT
        denom = limit if limit > 0 else ref
        if denom <= 0:
            return 0.0
        day = today_key(now)
        if _DAY_BASELINE.get("day") != day:
            _DAY_BASELINE["day"] = day
            _DAY_BASELINE["used"] = used
        spent_today = max(0, used - int(_DAY_BASELINE.get("used", 0) or 0))
        return max(0.0, min(1.0, spent_today / denom))
    except Exception:  # noqa: BLE001 - 读不到 = 不施加压力
        warn("core/state/drivers.py:64 Exception", err=repr(__import__("sys").exc_info()[1]))
        return 0.0


#: 每小时整点的清醒度（0~1）。这是"她的作息"（设定），但**时钟是真的**。
_HOURLY_ALERTNESS = (
    0.15, 0.10, 0.10, 0.10, 0.10, 0.15,   # 00-05 深夜
    0.35, 0.55, 0.80, 0.95, 1.00, 0.95,   # 06-11 起床 -> 上午
    0.75, 0.70, 0.85, 0.95, 1.00, 0.95,   # 12-17 午间 -> 下午
    0.90, 0.85, 0.75, 0.60, 0.40, 0.25,   # 18-23 傍晚 -> 深夜
)


def alertness(now: float | None = None) -> float:
    """按本地时钟给 0~1 的清醒度（她的作息）。真量 = **时钟**。

    与既有 ``quiet_hours``（23-7 二元硬拦，``core/temporal.py``）的关系：
    本函数是**连续**曲线，覆盖 18-23 等"允许主动、但该越来越收敛"的时段；
    两者**叠加**——quiet_hours 是硬门（拦死不发），这里只调**概率**。

    读不到时间 ⇒ ``1.0``（不施加影响）。

    **试验后门（默认关）**：设了 ``YINYUE_STATE_ALERTNESS_OVERRIDE`` 就直接用它、
    不看时钟 —— 仅用于「白天 vs 夜晚」对照测试（否则要等真白天）。生产不设该
    变量 ⇒ 完全走时钟；测完删掉 ``.env`` 里那行即可。
    """
    import os

    _override = text("YINYUE_STATE_ALERTNESS_OVERRIDE").strip()
    if _override:
        try:
            return max(0.0, min(1.0, float(_override)))
        except ValueError:
            warn("core/state/drivers.py:96 ValueError", err=repr(__import__("sys").exc_info()[1]))
    try:
        ts = float(now if now is not None else time.time())
        local = datetime.fromtimestamp(ts)
        a = _HOURLY_ALERTNESS[local.hour]
        b = _HOURLY_ALERTNESS[(local.hour + 1) % 24]
        return max(0.0, min(1.0, a + (b - a) * (local.minute / 60.0)))
    except Exception:  # noqa: BLE001 - 读不到时间 = 不施加影响
        warn("core/state/drivers.py:104 Exception", err=repr(__import__("sys").exc_info()[1]))
        return 1.0
