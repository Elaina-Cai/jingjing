# 旧决策层删除清单（2026-10-24 起）

恢复点：`git tag legacy-brain-2026-10-24`
归档副本：`_legacy_brain/`（不在 git 里，仅供查阅）

| 文件 | 行数/大小 | 替代者 | 状态 |
|---|---|---|---|
| `agents/conversational.py` | 192 KB | `loop/brain.py` | 待删 |
| `core/routing/turn_pipeline.py` | 2225 行 / 98 KB | `loop/brain.py` + `loop/tick.py` | 待删 |
| `core/routing/turn_decision.py` | 14 KB | 她自己的 `act` 决定 | 待删 |
| `core/routing/router.py` | 1242 行 / 53 KB | `loop/tick.py` | 待删 |
| `core/context/assembler.py` + `budget.py` + `prompt_layers.py` | 56 KB | `loop/snapshot.py` | 保留观察 |
| `core/group_state.py` | 140 KB | 全局视界 + 冷却 | 保留观察 |

要改的 import 点（实测只有这些）：

| 文件 | 行 | 内容 |
|---|---|---|
| `scheduler/main.py` | 49 | `from agents.conversational import ConversationalAgent` |
| `scheduler/main.py` | 99 | `_AGENT_REGISTRY` 里的 conversational 条目 |
| `scheduler/main.py` | 663 | `from core.routing.router import MessageRouter` |
| `core/bootstrap.py` | 607 | `from agents.conversational import ConversationalAgent` |
| `core/routing/__init__.py` | 1 | `from core.routing.router import *` |
| `agents/proactive_trigger.py` | 236 | `from core.routing.router import get_or_create_session_for_chat` |
| `web/routes_api.py` | 389/465/777 | `build_agent()` |

**不删**（驱动层，删了系统立刻哑）：`core/speech/outbound.py`(77 个依赖)、`core/execution/*`、`core/ingestion/*`、`db/*`、`llm/*`、`core/speech/guards.py`、`core/speech/rhythm.py`。
