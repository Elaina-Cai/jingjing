"""Native tool-calling regression tests.

The agent, not a keyword router, must own the decision to call a tool.  These
tests use a fake LLM so they do not need network or API keys.
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# 数据库隔离（照 tests/test_event_chain_links.py / test_command_service_db.py）：
# 本文件会经 core.bootstrap.init_singletons() 触发 init_meta_db + run_migrations
# （bootstrap 兜底、runtime 访问器等），不隔离就把 pending 迁移落到**真实**
# meta / account 库。2026-09-29 实测：Migration 51 被本套测试落到了生产库
# （data/yinyue_meta.db + 4 个账号库，applied_at 14:58:19Z）。必须在 import core.*
# 之前设好：PathSettings 在 core.config.settings **首次**导入时就取值。
# ---------------------------------------------------------------------------
_ISO_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "_test_agent_tool_calling_iso"
)
os.makedirs(_ISO_ROOT, exist_ok=True)
os.environ["META_DB_PATH"] = os.path.join(_ISO_ROOT, "meta.db")
os.environ["PLATFORMS_DIR"] = os.path.join(_ISO_ROOT, "platforms")
os.environ["DATA_DIR"] = os.path.join(_ISO_ROOT, "data")

from core.config.settings import config as _config  # noqa: E402

# 守卫：env 只在 core.config.settings 首次导入前设好才有效，否则真实路径已经
# 固化进 config。这里硬失败，绝不静默把迁移/写操作落到生产库。
assert os.path.realpath(_config.paths.meta_db_path) != os.path.realpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "yinyue_meta.db"
    )
), f"测试数据库隔离失效：meta_db_path={_config.paths.meta_db_path}"


from agents.tools.base import BaseTool, ToolRegistry
from llm.client import LLMClient


class _FakeProfileTool(BaseTool):
    name = "get_person_profile"
    description = "query person profile"
    parameters = {
        "person": {
            "type": "string",
            "description": "wxid or nickname",
            "required": True,
        },
    }

    async def execute(self, **kwargs):
        return None


class _FakeToolCallLLM:
    async def chat_tools(self, **kwargs):
        return {
            "content": "",
            "tool_calls": [{
                "id": "call_1",
                "name": "get_person_profile",
                "arguments": {"person": "P6", "query": "她说过什么"},
            }],
            "provider": "fake",
        }

    async def chat(self, **kwargs):
        return ""


class _FakeNoToolLLM:
    async def chat_tools(self, **kwargs):
        return {"content": "直接回答", "tool_calls": [], "provider": "fake"}

    async def chat(self, **kwargs):
        return ""


def test_tool_schema_is_openai_compatible() -> None:
    tool = _FakeProfileTool()
    schema = tool.schema()
    params = schema["function"]["parameters"]
    assert params["required"] == ["person"]
    assert "required" not in params["properties"]["person"]
    assert params["properties"]["person"]["type"] == "string"


def test_parse_json_extracts_object_from_prose() -> None:
    parsed = LLMClient.parse_json(
        '思考如下：{"thought": "ok", "action": null, "action_input": null} 完毕',
        {},
    )
    assert parsed["thought"] == "ok"
    assert parsed["action"] is None


def test_agent_uses_native_tool_call_decision() -> None:
    registry = ToolRegistry()
    registry.register(_FakeProfileTool())
    from agents.conversational import ConversationalAgent

    agent = ConversationalAgent()
    agent.inject(llm=_FakeToolCallLLM())
    agent.set_tool_registry(registry)
    step = asyncio.run(agent.think(
        "用户问：她和你说过什么话你记得不",
        SimpleNamespace(),
    ))
    assert step.action == "get_person_profile"
    assert step.action_input["person"] == "P6"


def test_agent_can_decide_no_tool() -> None:
    registry = ToolRegistry()
    registry.register(_FakeProfileTool())
    from agents.conversational import ConversationalAgent

    agent = ConversationalAgent()
    agent.inject(llm=_FakeNoToolLLM())
    agent.set_tool_registry(registry)
    step = asyncio.run(agent.think("今天几号", SimpleNamespace()))
    assert step.action is None
    assert "直接回答" in step.thought


class _CapturingLLM:
    """把系统提示词留下来（判定规则真的进了工具调用的 system）。"""

    def __init__(self) -> None:
        self.system = ""

    async def chat_tools(self, **kwargs):
        self.system = str(kwargs.get("system") or "")
        return {"content": "直接回答", "tool_calls": [], "provider": "fake"}

    async def chat(self, **kwargs):
        return ""


def test_creative_material_rule_is_in_tool_prompt() -> None:
    """2026-10-11：要创作素材（笑话/段子）→ 先 web_search 找现成素材，别拿重复请求当梗。

    依据：同一句「讲个笑话」连着问，她三次都拿"又被要求讲笑话"本身当梗
    （狼/主人/「第三遍了哦」）——因为记忆里没有任何笑话素材，只能拿对话本身当内容。
    """
    registry = ToolRegistry()
    registry.register(_FakeProfileTool())
    from agents.conversational import ConversationalAgent

    llm = _CapturingLLM()
    agent = ConversationalAgent()
    agent.inject(llm=llm)
    agent.set_tool_registry(registry)
    asyncio.run(agent.think("讲个笑话", SimpleNamespace()))
    assert "web_search" in llm.system
    assert "创作素材" in llm.system, llm.system[:300]
    assert "重复请求" in llm.system, "要写明：别拿「你又让我讲」这类重复请求当梗"


def test_creative_material_rule_is_in_main_prompt() -> None:
    """主链的 user prompt（``_build_research_prompt``）才是日常入口。

    2026-10-11 教训：同一条规则先只加进 ``think()`` 的 system（旧 ReAct 路径），
    主链看不到 → 她一次都没搜。这里把主链 prompt 也钉住。
    """
    from agents.conversational import ConversationalAgent

    agent = ConversationalAgent()
    prompt = agent._build_research_prompt("讲个笑话", "", "")
    assert "web_search" in prompt
    assert "创作素材" in prompt, prompt[-400:]
    assert "重复请求" in prompt, "要写明：别拿「你又让我讲」这类重复请求当梗"


def main() -> bool:
    tests = [
        test_tool_schema_is_openai_compatible,
        test_parse_json_extracts_object_from_prose,
        test_agent_uses_native_tool_call_decision,
        test_agent_can_decide_no_tool,
        test_creative_material_rule_is_in_tool_prompt,
        test_creative_material_rule_is_in_main_prompt,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"agent tool calling checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
