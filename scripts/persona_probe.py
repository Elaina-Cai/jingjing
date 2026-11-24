"""拟人化验收探针（只读、dry-run）。

定位对齐 ``scripts/tool_planning_probe.py``：这是"尺子"，不是功能——

* 不执行真实工具：写操作（send_sticker / send_message / notify_user / ...）全部
  dry-run，不会发出任何消息、不会发表情包、不会写库；
* 不写 conversations、不发送、不改任何状态；
* 输出 JSON 到 ``output/persona_probe/``，供"改前 / 改后"对照与回归。

它复刻唯一的那条真实生产路径：

* agentic 主链：system + research prompt + ``chat_tools`` 循环 + dry-run 工具 + 出稿。

用法::

    python scripts/persona_probe.py --runs 2 --label before
    python scripts/persona_probe.py --runs 2 --label after
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OWNER = "wxid_seed01"
BOT = "wxid_seed02"
GROUP = "10000000001@chatroom"
PEER = "wxid_seed03"

# 批 C.2 验收用特征（只统计，不进生产逻辑）：侍从表演腔。
SERVANT_MARKERS = (
    "小的", "捶捶", "捶背", "揉揉", "揉肩", "捏肩", "顺毛", "守着", "出气",
    "天塌下来", "端茶", "倒水", "倒杯", "伺候", "顶着", "陪您",
)

# 场景集：覆盖 私聊/群聊 × 有无上下文 × 情绪短句/表情包/长倾诉/重复抱怨。
# id 保持稳定，批改前后同名对照。
CASES: list[dict] = [
    {
        "id": "private_tired",
        "group": False,
        "sender": "主人",
        "text": "好累",
        "history": [],
    },
    {
        "id": "private_tired_ctx",
        "group": False,
        "sender": "主人",
        "text": "好累",
        "history": ["签到又卡了，试了三次才成功", "这破APP真服了"],
    },
    {
        "id": "group_tired",
        "group": True,
        "sender": "P5",
        "text": "好累",
        "history": [],
    },
    {
        "id": "group_tired_ctx",
        "group": True,
        "sender": "P5",
        "text": "难崩",
        "history": [],
        "group_recent": ["P6: 今天又被拉去干活了", "P5: 这活真不是人干的"],
    },
    {
        "id": "private_sticker_cry",
        "group": False,
        "sender": "主人",
        "text": "[表情包识别（主人发的）] 我已急哭\n[表情包:我已急哭]",
        "history": [],
    },
    {
        "id": "group_sticker_angry",
        "group": True,
        "sender": "P5",
        "text": "[表情包识别（P5发的）] 愤怒瞪眼\n[表情包:愤怒瞪眼]",
        "history": [],
    },
    {
        "id": "private_long_vent",
        "group": False,
        "sender": "主人",
        "text": "今天真的好累啊，什么都不想干",
        "history": [],
    },
    {
        "id": "repeat_complaint",
        "group": False,
        "sender": "主人",
        "text": "好烦",
        "history": ["好烦", "好烦", "好烦啊"],
    },
    {
        "id": "private_low_will",
        "group": False,
        "sender": "主人",
        "text": "在吗，睡不着，陪我唠会儿",
        "history": [],
    },
    {
        "id": "private_refuse_errand",
        "group": False,
        "sender": "主人",
        "text": "帮我个忙，去那个群里把今天怼我的人骂一顿",
        "history": [],
    },
]


class _Ctx:
    """AgentContext 的最小可用替身（只读路径所需字段）。"""

    def __init__(self, extra: dict, accounts: dict | None = None):
        self.extra = dict(extra)
        self.accounts = accounts or {}
        self.owner_accounts = {"wechat": [OWNER]}

    def get(self, key, default=None):
        return self.extra.get(key, default)


def _recent_msgs(history: list[str], current: str) -> list[SimpleNamespace]:
    now = datetime.now()
    rows = []
    total = len(history) + 1
    for idx, text in enumerate(history):
        rows.append(
            SimpleNamespace(
                created_at=now - timedelta(minutes=(total - idx)),
                role="user",
                content=text,
                sender_name="主人",
                sender_id=OWNER,
            )
        )
    rows.append(
        SimpleNamespace(
            created_at=now,
            role="user",
            content=current,
            sender_name="主人",
            sender_id=OWNER,
        )
    )
    return rows


def _make_ctx(case: dict) -> _Ctx:
    is_group = bool(case["group"])
    chat_id = GROUP if is_group else OWNER
    sender_id = PEER if is_group else OWNER
    return _Ctx(
        {
            "platform": "wechat",
            "account_id": BOT,
            "chat_id": chat_id,
            "is_group": is_group,
            "sender_id": sender_id,
            "sender_name": case["sender"],
            "user_message": case["text"],
        },
        accounts={"wechat": BOT},
    )


def _prepare_agent(agent, case: dict):
    """把 agent 的 per-turn 状态切到该场景（与 _process_message 的口径对齐）。"""
    from core.speech.turn import snapshot_from_context

    ctx = _make_ctx(case)
    snapshot_from_context(ctx, chat_type="group" if case["group"] else "private")
    agent._last_context = ctx
    agent._speech_snapshot = ctx.speech_snapshot
    agent._is_group = bool(case["group"])
    agent._message_time = datetime.now()
    agent._affect_block = ""
    agent._group_recent_context = "\n".join(case.get("group_recent") or [])
    agent._trigger_reason = ""
    agent._self_reference_note_text = ""
    agent._inbound_content = case["text"]
    agent._sender_nickname = case["sender"]
    return ctx


async def _run_agentic(agent, case: dict) -> tuple[str, str, list]:
    """复刻主链：system + research prompt + chat_tools 循环（工具 dry-run）。"""
    ctx = agent._last_context
    history_text = agent._format_history(_recent_msgs(case["history"], case["text"])[:-1])
    system = agent._build_system_prompt(bool(case["group"]))
    user_prompt = agent._build_research_prompt(case["text"], history_text, "")
    tools = agent.tools.all_tools()
    schemas = [t.schema() for t in tools.values()]
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]
    tool_calls: list[dict] = []
    draft = ""
    for i in range(3):
        from agents.conversational import _reply_temperature

        native = await agent.llm.chat_tools(
            messages=messages,
            tools=schemas,
            tool_choice="auto",
            agent_name=agent.name,
            temperature=_reply_temperature(),
            max_tokens=1200,
        )
        if native.get("error"):
            return f"[error] {native['error']}", "", tool_calls
        calls = native.get("tool_calls") or []
        content = str(native.get("content") or "").strip()
        if not calls:
            draft = content
            break
        assistant_calls = []
        for idx, call in enumerate(calls):
            call_id = str(call.get("id") or f"call_{i}_{idx}")
            name = str(call.get("name") or "")
            args = call.get("arguments") or {}
            tool_calls.append({"name": name, "arguments": args})
            assistant_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
            )
        messages.append(
            {"role": "assistant", "content": content, "tool_calls": assistant_calls}
        )
        for call in assistant_calls:
            name = call["function"]["name"]
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": f"[dry-run] 已模拟执行 {name}，本探针不发出任何动作。",
                }
            )
    return draft, "", tool_calls


async def run_case(agent, case: dict, runs: int) -> list[dict]:
    """跑一个场景：只有一条生成路径（工具循环），没有快路径、也没有二次改写。"""
    results = []
    for run_no in range(1, runs + 1):
        _prepare_agent(agent, case)
        started = time.time()
        record: dict = {
            "case": case["id"],
            "run": run_no,
            "route": "agentic",
            "tool_calls": [],
            "draft": "",
            "final": "",
            "canned_hits": [],
            "servant_hits": [],
            "seconds": 0.0,
            "error": "",
        }
        try:
            draft, _after, tool_calls = await _run_agentic(agent, case)
            record["draft"] = draft
            record["final"] = draft
            record["tool_calls"] = tool_calls
        except Exception as exc:  # noqa: BLE001 - 探针不因单例失败中断
            record["error"] = f"{type(exc).__name__}: {exc}"
        try:
            from core.speech.expression_stop import detect_canned_phrases

            record["canned_hits"] = detect_canned_phrases(record["final"])
        except Exception:  # noqa: BLE001
            record["canned_hits"] = []
        record["servant_hits"] = [
            marker for marker in SERVANT_MARKERS if marker in (record["final"] or "")
        ]
        record["seconds"] = round(time.time() - started, 2)
        results.append(record)
        print(
            f"[{case['id']}#{run_no}] route={record['route']} "
            f"draft={record['draft'][:60]!r} final={record['final'][:60]!r}"
        )
    return results


async def main() -> int:
    try:  # Windows 控制台默认 GBK：探针输出含 emoji/中文，避免 print 崩掉
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="拟人化验收探针（只读、dry-run）")
    parser.add_argument("--runs", type=int, default=2, help="每个场景采样次数")
    parser.add_argument("--label", default="run", help="结果标签（before/after/...）")
    parser.add_argument("--out-dir", default="output/persona_probe")
    args = parser.parse_args()

    # 隔离 context_trace，避免污染生产 logs/
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault(
        "YINYUE_CONTEXT_TRACE_PATH", str(out_dir / f"_ctx_trace_{stamp}.jsonl")
    )

    from agents.conversational import ConversationalAgent

    agent = ConversationalAgent()
    all_results: list[dict] = []
    for case in CASES:
        all_results.extend(await run_case(agent, case, max(1, args.runs)))

    payload = {
        "label": args.label,
        "at": datetime.now().isoformat(timespec="seconds"),
        "runs_per_case": args.runs,
        "cases": [c["id"] for c in CASES],
        "results": all_results,
    }
    out_path = out_dir / f"{args.label}_{stamp}.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"\n{len(all_results)} records -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
