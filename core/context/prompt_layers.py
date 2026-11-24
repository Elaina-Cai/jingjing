# -*- coding: utf-8 -*-
"""Prompt 只由「四层」构成——层归属表（2026-10-23 主人裁定后建的唯一账本）。

主人的四层（口头说法 → 本模块常量）：

* **自我立方体·第一层（人设）** → :data:`PERSONA`：她是谁、怎么说话、什么时候闭嘴。
  这一层**始终全量注入**（主人 2026-10-23 原话：「人设层始终全量注入」）。
* **自我立方体·第二/三层（情绪、态度）** → **不在这里**：2026-10-23 起改由模型自己填参调工具
  （`read_emotion` / `get_stance_cards`），一个字符都不进 prompt。
* **世界窗口** → :data:`WORLD`：此刻发生了什么（窗口、局况、指代、归属、位置、读场）。
* **记忆立方体** → :data:`MEMORY`：事件记忆、场外补充、名册、关系事实。
* **在手的事** → :data:`JOB`：正在办的事、本轮步骤、计划契约、工具清单。

这张表的作用**不是**分类癖：`tests/test_prompt_layers.py` 要求
`core/context/assembler.py::_BLOCK_POLICY` 里**每一条登记都必须恰好属于这里的一层**。
于是「又长出一块谁也不知道它属于哪一层的旧东西」这件事从「靠人记得」变成**单跑就红**。

新增一块时的正确做法：先想清楚它喂的是哪一层；想不清楚，就说明它不该存在。
"""
from __future__ import annotations

PERSONA = "persona（人设：我是谁、怎么说话、何时沉默）"
WORLD = "world（世界窗口：此刻发生了什么）"
MEMORY = "memory（记忆：记得什么）"
JOB = "job（在手的事：正在办什么、此刻能做什么）"

LAYERS: tuple[str, ...] = (PERSONA, WORLD, MEMORY, JOB)

#: 块头前缀 -> 所属层。前缀与 `assembler._BLOCK_POLICY` 的第一列逐字一致
#: （`_classify` 用 `first.startswith(prefix)` 匹配块第一行）。
BLOCK_LAYER: dict[str, str] = {
    "[人格]": PERSONA,
    # ── 人设层：她是谁、怎么说话、什么时候闭嘴 ─────────────────────────────
    "[强制规则]": PERSONA,
    "[主动触达规则]": PERSONA,
    "[纠错确认]": PERSONA,
    "[你说过·作数]": PERSONA,
    # ── 世界窗口：此刻发生了什么 ────────────────────────────────────────
    "[世界窗口]": WORLD,
    "[局况]": WORLD,
    "[当前局面]": WORLD,
    "[时间感知]": WORLD,
    "[指代事实]": WORLD,
    "[消息归属]": WORLD,
    "[位置]": WORLD,
    "[现在这一屏]": WORLD,
    "[群聊最近消息": WORLD,
    "[最近对话": WORLD,
    "[话题簇与锚点]": WORLD,
    # ── 记忆：记得什么 ───────────────────────────────────────────────
    "[事件记忆": MEMORY,
    "[按需记忆]": MEMORY,
    "[场外补充]": MEMORY,
    "[群成员名单": MEMORY,
    "[关系事实]": MEMORY,
    "[未完的事]": MEMORY,
    # ── 在手的事：正在办什么、此刻能做什么 ────────────────────────────
    "[在手的事]": JOB,
    "[本轮步骤]": JOB,
    "要真办就附": JOB,
    "[本轮可用的工具]": JOB,
}

#: 2026-10-23 主人点名删掉的块 —— 产者已从仓库里删除，块头前缀留在这里是为了
#: 让守卫能区分「删干净了」和「忘了」：出现在这里 = 已退役（不许再登记、不许再产出）。
RETIRED_BLOCKS: dict[str, str] = {
    "[基本表达]": "输出渠道契约已并入 persona 的语体设定，块本身退役",
    "[不确定与沉默]": "沉默/不确定规矩退役：要不要出声交给工具契约与判",
    "[沉默协议]": "与 [不确定与沉默] 合并后被一起删掉",
    "[拿不准时]": "与 [不确定与沉默] 合并后被一起删掉",
    "[安全与边界]": "policy 的 brief/hard_constraints 不再渲染成块",
    "[本轮理解]": "N1 判词不再进 system：判断改走 user 侧感知卡（Step 2a）与会话内 perceive（Step 2b）",
    "[读场]": "读场结论不再进 system：N1 的 semantic_reads 由感知卡/工具取代",
    "[语气风格]": "分场景示例与语气目标不再注入（语气走 voice 注入与工具）",
    "[遇事怎么反应]": "反应清单退役（REACT_PLAYBOOK 的渲染块）",
}

__all__ = ["PERSONA", "WORLD", "MEMORY", "JOB", "LAYERS", "BLOCK_LAYER", "RETIRED_BLOCKS",
           "layer_of", "blocks_of", "is_retired"]


def layer_of(prefix: str) -> str:
    """块头属于哪一层；没登记返回空串（守卫会把它标红）。"""
    return BLOCK_LAYER.get(str(prefix or ""), "")


def blocks_of(layer: str) -> tuple[str, ...]:
    return tuple(prefix for prefix, name in BLOCK_LAYER.items() if name == layer)


def is_retired(prefix: str) -> bool:
    """这个块头是不是已经退役（产者删了，前缀留作记账）。"""
    return str(prefix or "") in RETIRED_BLOCKS
