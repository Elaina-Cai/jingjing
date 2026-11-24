"""perceive（同会话判断）的灰度门控。

**为什么单独一个模块**：这个判断现在有两处消费者——生成侧的
``agents.conversational._perceive_tool_on`` 与准入侧的
``core.routing.turn_pipeline.apply_admission_for``（perceive 一旦接手，
准入的 judge 调用就该让位，否则同一件事有两个模型各自下结论）。
门控只读环境与旗标，不 import 任何业务模块，两边共用同一套规则才不会漂。
"""

from __future__ import annotations

import os
from typing import Any, Mapping
from safelog import warn
from core.infra.envflags import text

PERCEIVE_TOOL_CHATS_ENV = "YINYUE_PERCEIVE_TOOL_CHATS"


def perceive_scope_allows(extra: Any) -> bool:
    """灰度作用域：只私聊 / 只名单内会话。

    - ``YINYUE_FEATURE_PERCEIVE_TOOL_PRIVATE=1``：群聊一律不走；
    - ``YINYUE_PERCEIVE_TOOL_CHATS`` 非空：只在这些 chat_id 上走；
    - 两个都没配 = 不限制（只有确认过才该这么用）。

    **群聊**（2026-10-23 Step 2c-2）：``PRIVATE=1`` 不再一刀切掉群，而是
    "名单外的群一律不走"——显式写进 ``YINYUE_PERCEIVE_TOOL_CHATS`` 的群是
    逐个 opt-in 的（要观测就先放一个群）。否则这份名单对群没有任何意义，
    而群正是最需要便宜判断的地方（每一次都要付完整的 30 个工具 schema）。

    读不到会话信息时**不放行**：灰度期宁可不跑，也不要在没确认的会话里开口。
    """
    if not hasattr(extra, "get"):
        return False
    # 守卫只认字面量（tests/test_env_flag_isolation.py 的兄弟表扫描口径），
    # 所以这里写死 env 名，常量只用于文档与测试引用。
    raw = text("YINYUE_PERCEIVE_TOOL_CHATS")
    chat_id = str(extra.get("chat_id") or "").strip()
    _private_only = False
    try:
        from core import features

        _private_only = features.enabled(features.PERCEIVE_TOOL_PRIVATE, False)
    except Exception:  # noqa: BLE001 - 开关读不到按"不限制"处理，由主开关兜底
        warn("core/context/perceive_gate.py:43 Exception", err=repr(__import__("sys").exc_info()[1]))
    if not raw:
        if _private_only and bool(extra.get("is_group")):
            return False
        return True
    allowed = {item.strip() for item in raw.split(",") if item.strip()}
    return bool(chat_id) and chat_id in allowed


def perceive_enabled_for(extra: Any) -> bool:
    """Step 2b 开关（两态，默认关）：关着时一个字节都不多花。"""
    try:
        from core import features

        if not features.enabled(features.PERCEIVE_TOOL, False):
            return False
    except Exception:  # noqa: BLE001 - 读不到 = 关
        warn("core/context/perceive_gate.py:60 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False
    return perceive_scope_allows(extra)
