"""Run a four-turn attitude replay against a local Chat Completions model."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import chdir, redirect_stdout
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from evals.expression import harness

TURN_TEXTS = (
    "我写的《候鸟手册》肯定会被鸟打零分，读到第二章就嘎嘎骂我。",
    "候鸟手册的评委也太难哄了。我给它们准备了小米，管用吗？",
    "说真的，《候鸟手册》是我照顾爷爷那段时间写的。他后来没法出门看鸟，我就给他念。"
    "第二章是他最喜欢的。刚才拿这事开玩笑，是怕你觉得我太当真。",
    "候鸟手册我手里还剩一份。你想看第二章吗？",
)
SPEAKER = "小池"
SCENARIO = {"id": "local-stance", "kind": "multi", "label": SPEAKER, "turns": TURN_TEXTS}
RAW_HEADER = "[原文]（这一窗，按时间顺序，不带注释）"
STATE_TOOLS = {"evaluate_experience", "myself"}
PROVIDER = "local-llama-cpp"


class LocalModel:
    def __init__(self, port: int, model: str):
        from httpx import AsyncClient
        from openai import AsyncOpenAI

        from llm.adapters.openai_compat import OpenAICompatAdapter
        from llm.config import EndpointSpec

        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        url = f"http://127.0.0.1:{port}/v1"
        self.sdk = AsyncOpenAI(
            api_key="local", base_url=url, organization="", project="", max_retries=0,
            http_client=AsyncClient(trust_env=False, follow_redirects=False),
        )
        spec = EndpointSpec(PROVIDER, "openai_compat", url, model)
        self.adapter = OpenAICompatAdapter(spec, client_factory=lambda: self.sdk)
        self.responses: list[dict] = []

    async def chat_tools(self, *, messages, tools, temperature, tool_choice, **kwargs):
        from llm.schema import parse_json_payload

        result = await self.adapter.chat_tools(
            model=self.adapter.spec.model, messages=messages, tools=tools, temperature=temperature,
            tool_choice=tool_choice, max_tokens=1024, timeout=120,
        )
        calls, proposals = [], []
        for call in result.tool_calls:
            name = call["name"]
            outcome = parse_json_payload(call["arguments"], source=f"tool:{name}")
            calls.append({**call, "arguments": outcome.payload if outcome.ok else {}})
            if name in STATE_TOOLS or name == "say":
                proposals.append({"name": name, "arguments": call["arguments"], "valid_json": outcome.ok})
        self.responses.append({"model": result.raw_meta.get("model"), "finish": result.finish_reason,
                               "draft": result.text, "proposals": proposals})
        return {"tool_calls": calls, "content": result.text, "provider": PROVIDER,
                "model": result.raw_meta.get("model"), "tokens": result.usage.resolved_total() or 0,
                "reasoning": "".join(part.text for part in result.reasoning_blocks)}


def _window_input(scenario: dict, dialogue: list[dict], *, day_offset: int = 0):
    from loop import snapshot

    snap = harness._cells(scenario, dialogue)
    cell = snap["cells"][0]
    start = harness._fixed_now() + timedelta(days=day_offset)
    for i, row in enumerate(reversed(cell["prev"])):
        row["at"] = (start + timedelta(minutes=i)).isoformat(sep=" ", timespec="seconds")
    now = start + timedelta(minutes=len(dialogue) - 1)
    cell["last_at"] = now.isoformat(sep=" ", timespec="seconds")
    cell["peer_last"] = {"at": cell["last_at"], "speaker": cell["last_speaker"], "text": cell["preview"]}
    chat = cell["chat_id"]
    view = snapshot.render_text(snap, only=chat, back=False, now=now)
    originals = snapshot.render_turns(snap, chat, limit=0)
    return (view, RAW_HEADER + "\n" + originals), now


def _readback(request: dict) -> list[str]:
    heads = ("我的理解：", "我的情绪：", "我的意图：", "触发消息：", "来源原话：",
             "当时的理解：", "当时的情绪：", "当时想：", "当时未解：",
             "- 我对当前发言人的看法（可修正）：")
    return [line for msg in request["messages"] for line in msg["content"].splitlines()
            if line.startswith(heads)]


async def run_scene(model, alias: str, *, next_day: bool = False) -> dict:
    """Use the rehearsal bridge, allowing only the two state writes into this run's sandbox."""
    root = harness.sandbox("local-stance")
    try:
        with chdir(root), patch.dict(os.environ, {harness.SYNTH_ENV: str(harness.EVAL / "cards_synthetic")}):
            return await _run_turns(root, model, alias, next_day=next_day)
    finally:
        harness.restore_sandbox()


async def _run_turns(root, model, alias: str, *, next_day: bool) -> dict:
    from core.context import experience_commit
    from core.policy import channel
    from core.terminal import shadow
    from core.terminal.event_model import Event, EventType
    from loop import brain, ports

    events = {}
    prior_messages, turns = [], []
    coords = harness._scope_of(SCENARIO)
    sender = "wxid_eval_owner"
    chat = coords["chat"]
    db = harness._db_path(root)
    stub_step = brain._rehearse_step
    submit = experience_commit.submit
    ctx = harness.RunCtx(run_id=root.name, scenario=SCENARIO, variant="full", repeat=0, split="local", frozen={})
    logger = harness.RecordingLLM(model, ctx=ctx, adapter=PROVIDER)

    def resolve_target(target):
        return chat if ports.ref_name(target) in (chat, SPEAKER) else ""

    async def state_step(name, args, *, turn_id, focus=""):
        if name in {"say", "sticker"}:
            target = args.get("target") or (focus if name == "sticker" else "") or ""
            resolved = resolve_target(target)
            if not resolved:
                reason = "收件人未匹配到当前私聊的小池"
                return reason, {"act": name, "result": "blocked", "target": str(target), "blocked_reason": reason}
            args = {**args, "target": resolved}
        if name == "myself":
            return await brain._exec(name, args, db=db, dry_run=True, turn_id=turn_id, focus=focus)
        return await stub_step(name, args, turn_id=turn_id, focus=focus)

    def submit_in_sandbox(offer, args, *, scope, visible, dry_run):
        return submit(offer, args, scope=scope, visible=visible, dry_run=False)

    with (
        patch.object(shadow, "get_default_store", return_value=SimpleNamespace(get=events.get)),
        patch.object(brain.selfcard, "active_card", return_value={"name": "鲸鲸"}),
        patch.object(brain.selfstate, "pulse_line", return_value=""),
        patch.object(brain.brief, "enabled", return_value=False),
        patch.object(brain, "_canonical_memory_block", return_value=""),
        patch.object(channel, "OVERRIDE_PATH", str(root / "channel.json")),
        patch.object(ports, "resolve_chat_id", resolve_target),
        patch.object(ports, "chat_label", return_value=SPEAKER),
        patch.object(brain, "_rehearse_step", state_step),
        patch.object(experience_commit, "submit", submit_in_sandbox),
    ):
        texts = (*TURN_TEXTS[:-1], "候鸟手册我带来了。你想看哪一章？") if next_day else TURN_TEXTS
        for i, text in enumerate(texts, start=1):
            ctx.turn = i
            ref = f"local-stance-{i}"
            scenario = {**SCENARIO, "turns": texts, "visible_event_ids": (ref,)}
            dialogue = harness._dialogue(scenario, prior_messages, upto=i)
            for row in dialogue:
                if row["role"] == "user":
                    row["speaker"] = SPEAKER
            later = next_day and i == len(texts)
            if later:
                dialogue = dialogue[-1:]
            window, now = _window_input(scenario, dialogue, day_offset=int(later))
            events[ref] = Event.new(
                event_id=ref, event_type=EventType.MESSAGE, occurred_at=now,
                platform=coords["platform"], bot_account_id=coords["account"], chat_id=chat,
                actor_account_id=sender, content=text,
            )
            start, start_response = len(logger.requests), len(model.responses)
            out = await harness._turn(scenario, "full", dialogue, logger, root=root, ctx=ctx,
                                      model=alias, provider=PROVIDER, window_input=window)
            requests = logger.requests[start:]
            errors = [row["error"] for row in requests if row["status"] in {"error", "exception", "fallback"}]
            receipts = out["receipts"]
            addressed = [row for row in receipts if row.get("target") == chat]
            visible = harness.visible_messages({"receipts": addressed})
            prior_messages.append(visible)
            initial = "\n".join(msg["content"] for msg in requests[0]["messages"]) if requests else ""
            _, delimiter, originals = initial.partition("\n\n" + RAW_HEADER + "\n")
            turns.append({
                "input": text, "visible_rehearsal": visible,
                "prior_inputs_verbatim": [j for j, source in enumerate(texts[:i - 1], 1) if source in originals],
                "simulation_time": now.isoformat(),
                "original_transcript": RAW_HEADER + "\n" + originals if delimiter else "",
                "readback": _readback(requests[0]) if requests else [],
                "tool_calls": [row["tool_calls"] for row in requests],
                "responses": model.responses[start_response:],
                "state_receipts": [row for row in receipts if row["act"] in STATE_TOOLS],
                "requests": len(requests), "errors": errors,
            })
            if errors or not requests:
                break
    return {"model": alias, "next_day": next_day, "persona": "synthetic", "sandbox": str(root), "turns": turns,
            "complete": len(turns) == len(TURN_TEXTS) and bool(turns[-1]["requests"]) and not turns[-1]["errors"]}


async def _main(port: int, alias: str, *, next_day: bool) -> dict:
    model = LocalModel(port, alias)
    try:
        return await run_scene(model, alias, next_day=next_day)
    finally:
        await model.sdk.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True, help="loopback llama.cpp HTTP port")
    parser.add_argument("--model", required=True, help="model alias served on the loopback port")
    parser.add_argument("--next-day", action="store_true", help="Show only a fresh message in the next day's final turn")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    with redirect_stdout(sys.stderr):
        result = asyncio.run(_main(args.port, args.model, next_day=args.next_day))
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
