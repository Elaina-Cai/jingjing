"""回合九步：名字是契约，顺序是纪律。"""

from __future__ import annotations

STEP_NAMES: tuple[str, ...] = (
    "ingest",   # 收：把抵达的东西变成 Observation（纯代码，零模型）
    "read",     # 看：会话窗口 / 感官 / 读数（只读）
    "judge",    # 判：一次模型调用 -> Reading -> matrix -> 处置
    "record",   # 记：账本落行（发生什么）、政策更新（长期允许什么）
    "act",      # 办：工具 / 在手的事 / 设备租约
    "say",      # 说：出口 deliver()（唯一对外口）
    "receipt",  # 回执：六态落库（发没发出去）
    "metrics",  # 量：量具与每回合成本
    "finish",   # 收尾：释放资源、写统计
)

_STEP_INDEX = {name: i for i, name in enumerate(STEP_NAMES)}


def is_step(name: str) -> bool:
    """是不是一个合法步骤名。"""
    return name in _STEP_INDEX


def order_of(name: str) -> int:
    """步骤序号（不在九步里 -> -1）。"""
    return _STEP_INDEX.get(name, -1)


def before(a: str, b: str) -> bool:
    """a 是否严格排在 b 之前。"""
    return 0 <= order_of(a) < order_of(b)
