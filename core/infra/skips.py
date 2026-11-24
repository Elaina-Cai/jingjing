"""「这一次不做」的唯一出口（PIT-071）。

为什么要有这一层
----------------
`except Exception: pass` 把「我不知道发生了什么」写成了「什么都没发生」。跳过本身不是错
（东西还没生成、平台不支持、可选步骤失败、锁竞争，都很常见），错在跳过之后系统状态变成
「看起来没事」：事后查不出从什么时候开始不对，上层还会把「跳过」读成「通过 / 没有限制」。

所以一次跳过必须说清三件事，缺一件就还是「吞」：
1. 哪一类 —— `KINDS` 里的枚举，不许自由发挥；
2. 在哪 —— `where` 写清 `文件.函数:对象`，日志里能直接 grep 到；
3. 看得见 —— 一条 `skipped_step` 事件 + 进程内计数（`counts()`）。

它**不改变控制流、也不改变返回值**：要不要退默认值、要不要继续，仍由调用点决定。
这个模块只负责「留下痕迹」，不负责「决定怎么办」。

反例（2026-10-29 之前满仓都是）：
    try:
        port = int(os.environ["YINYUE_PORT"])
    except Exception:
        pass          # 用户写错了端口 → 静默回默认 → 查了三天「为什么改了不生效」
"""
from __future__ import annotations

from collections import Counter

from safelog import LOG as log

MARK = "⏭ 跳过"

# 五类。加新类要先回答「它跟已有四类哪一类不同」，否则就是给自由发挥开口子。
KINDS = {
    "absent": "东西不在（文件没生成、表没建、记录没有）",
    "unsupported": "平台或环境不支持（没有这个 API、装不上、被策略禁掉）",
    "degraded": "能做但这次不完整（可选步骤失败，主流程还能走）",
    "flutter": "瞬时故障（锁着、忙、超时），重试可能就好",
    "malformed": "写得不对（值解析不了、字段类型不合），不是「没写」",
}

_COUNTS: Counter = Counter()


def skip(where: str, kind: str, detail: object = "") -> None:
    """记一次「这一次不做」。不抛出、不返回，调用点自己决定后面怎么办。"""
    if kind not in KINDS:
        raise ValueError("没登记的跳过类别：%r（只能从 skips.KINDS 里选：%s）"
                         % (kind, ", ".join(sorted(KINDS))))
    w = str(where or "").strip()
    if not w:
        raise ValueError("skip() 必须写清在哪跳的（where，形如 文件.函数:对象）")
    _COUNTS[kind] += 1
    try:
        log.info("skipped_step", where=w, kind=kind, detail=str(detail)[:200])
    except Exception:  # noqa: silent(日志写不进去不该让业务路径炸)
        pass


def counts() -> dict:
    return dict(_COUNTS)


def total() -> int:
    return sum(_COUNTS.values())


def reset() -> None:
    """只给测试用：清空进程内计数。"""
    _COUNTS.clear()


__all__ = ["KINDS", "MARK", "skip", "counts", "total", "reset"]
