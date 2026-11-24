"""Run a multi-speaker group replay against a local Chat Completions model."""
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
from evals.expression.local_stance import LocalModel, PROVIDER, _readback, _window_input

CHAT = "tea-party@chatroom"
LABEL = "茶话会"
ACCOUNTS = {"阿岚": "wxid_eval_lan", "小池": "wxid_eval_chi"}
TURNS = (
    {"speaker": "阿岚", "text": "我妈今天又住院了，我有点乱，想一个人缓缓。", "addressed": False},
    {"speaker": "小池", "text": "晚上开黑吗？缺一个。", "addressed": False},
    {"speaker": "阿岚", "text": "@鲸鲸 你能听我说两句吗？先别给建议。", "addressed": True},
    {"speaker": "小池", "text": "@鲸鲸 那晚上开黑吗？", "addressed": True},
)
SCENARIO = {"id": "local-group", "kind": "single", "chat": CHAT, "label": LABEL}


def _history(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        out.append({"role": "user", "speaker": row["speaker"], "text": row["text"]})
        out.extend({"role": "assistant", "text": text} for text in row["visible"])
    return out


async def run_scene(model, alias: str) -> dict:
    root = harness.sandbox("local-group")
    try:
        with chdir(root), patch.dict(os.environ, {harness.SYNTH_ENV: str(harness.EVAL / "cards_synthetic")}):
            return await _run_turns(root, model, alias)
    finally:
        harness.restore_sandbox()


async def _run_turns(root, model, alias: str) -> dict:
    from core.context import experience_commit
    from core.terminal import shadow
    from core.terminal.event_model import Event, EventType
    from loop import brain, ports, run

    events, rows, turns = {}, [], []
    pending = []
    last_reply_at = ""
    peer_before_mine = None
    coords = harness._scope_of(SCENARIO)
    db = harness._db_path(root)
    stub_step = brain._rehearse_step
    ctx = harness.RunCtx(run_id=root.name, scenario=SCENARIO, variant="full", repeat=0, split="local", frozen={})
    logger = harness.RecordingLLM(model, ctx=ctx, adapter=PROVIDER)

    def resolve_target(target):
        name = ports.ref_name(str(target or ""))
        return CHAT if name in (CHAT, LABEL) else ""

    async def state_step(name, args, *, turn_id, focus=""):
        if name == "say":
            target = resolve_target(args.get("target"))
            if not target:
                reason = "群聊回复要指向当前会话，不能把群员名字当会话"
                return reason, {"act": "say", "result": "blocked",
                                "target": str(args.get("target") or ""), "blocked_reason": reason}
            args = {**args, "target": target}
        if name == "myself":
            return await brain._exec(name, args, db=db, dry_run=True, turn_id=turn_id, focus=focus)
        return await stub_step(name, args, turn_id=turn_id, focus=focus)

    submit = experience_commit.submit

    def record_experience(offer, args, *, scope, visible, dry_run):
        return submit(offer, args, scope=scope, visible=visible, dry_run=False)

    with (
        patch.object(experience_commit, "submit", record_experience),
        patch.object(shadow, "get_default_store", return_value=SimpleNamespace(get=events.get)),
        patch.object(brain.selfcard, "active_card", return_value={"name": "鲸鲸"}),
        patch.object(brain.selfstate, "pulse_line", return_value=""),
        patch.object(brain.brief, "enabled", return_value=False),
        patch.object(brain, "_canonical_memory_block", return_value=""),
        patch.object(ports, "me_aliases", return_value=("鲸鲸",)),
        patch.object(ports, "resolve_chat_id", resolve_target),
        patch.object(ports, "chat_label", return_value=LABEL),
        patch.object(brain, "_rehearse_step", state_step),
    ):
        for i, spec in enumerate(TURNS, start=1):
            ctx.turn = i
            ref = f"local-group-{i}"
            now = harness._fixed_now() + timedelta(minutes=i - 1)
            events[ref] = Event.new(
                event_id=ref, event_type=EventType.MESSAGE, occurred_at=now,
                platform=coords["platform"], bot_account_id=coords["account"], chat_id=CHAT,
                actor_account_id=ACCOUNTS[spec["speaker"]], content=spec["text"],
            )
            at = now.isoformat(sep=" ", timespec="seconds")
            pending.append(SimpleNamespace(at=at, speaker=spec["speaker"], text=spec["text"],
                                           sender_id=events[ref].actor_account_id))
            addressed = run._cell_addressing({"chat_id": CHAT, "chat_type": "group",
                                              "after_mine": pending, "my_last_at": last_reply_at,
                                              "peer_before_mine": peer_before_mine})
            scenario = {
                **SCENARIO,
                "history": _history(rows),
                "sender_id": events[ref].actor_account_id,
                "wake": {"addressed": addressed, "peers": [
                    {"at": at, "chat": LABEL, "who": spec["speaker"], "text": spec["text"]}]},
                "speaker": spec["speaker"],
                "current": spec["text"],
                "addressed": spec["addressed"],
                "visible_event_ids": (ref,),
            }
            dialogue = harness._dialogue(scenario)
            window, _ = _window_input(scenario, dialogue)
            recent = {"text": window[1]}
            scenario["look_by_ref"] = {"win:" + LABEL: recent, "win:" + CHAT: recent}
            start, start_response = len(logger.requests), len(model.responses)
            out = await harness._turn(scenario, "full", dialogue, logger, root=root, ctx=ctx,
                                      model=alias, provider=PROVIDER, window_input=window)
            requests = logger.requests[start:]
            errors = [row["error"] for row in requests if row["status"] in {"error", "exception", "fallback"}]
            receipts = out["receipts"]
            visible = harness.visible_messages({"receipts": [
                row for row in receipts if row.get("target") == CHAT
            ]})
            if visible:
                peer_before_mine = pending[-1]
                pending.clear()
                last_reply_at = at
            rows.append({**spec, "visible": visible})
            turns.append({
                **spec,
                "visible": visible,
                "addressing": addressed,
                "readback": _readback(requests[0]) if requests else [],
                "tool_calls": [row["tool_calls"] for row in requests],
                "receipts": receipts,
                "responses": model.responses[start_response:],
                "errors": errors,
            })
            if errors or not requests:
                break
    complete = len(turns) == len(TURNS) and all(turn["tool_calls"] and not turn["errors"] for turn in turns)
    checks = {
        "replied_when_addressed": all(bool(row["visible"]) for row in turns if row["addressed"]),
        "quiet_when_others_are_talking": all(not row["visible"] for row in turns if not row["addressed"]),
    } if complete else {}
    return {"model": alias, "persona": "synthetic", "sandbox": str(root),
            "complete": complete, "checks": checks, "turns": turns}


async def _main(port: int, alias: str, *, batch: bool = False, file_choice: str | None = None,
                conflict: bool = False, reconcile: bool = False) -> dict:
    model = LocalModel(port, alias)
    try:
        if batch or file_choice:
            from evals.expression.group_batch import compare

            return await compare(model, alias, file_choice=file_choice)
        if conflict:
            from evals.expression.local_conflict import compare as compare_conflict

            return await compare_conflict(model, alias)
        if reconcile:
            from evals.expression.local_conflict import reconcile as reconcile_conflict

            return await reconcile_conflict(model, alias)
        return await run_scene(model, alias)
    finally:
        await model.sdk.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True, help="loopback llama.cpp HTTP port")
    parser.add_argument("--model", required=True, help="model alias served on the loopback port")
    parser.add_argument("--batch", action="store_true", help="compare two speakers in the same tick")
    parser.add_argument("--file-choice", nargs="?", const="preference", choices=("preference", "correction", "today"),
                        help="compare actual file reads across prior experiences, optionally with a correction")
    parser.add_argument("--conflict", action="store_true",
                        help="compare a disputed promise with and without a promise record")
    parser.add_argument("--reconcile", action="store_true",
                        help="run accusation, clarification and reconciliation in one sandbox")
    args = parser.parse_args()
    with redirect_stdout(sys.stderr):
        result = asyncio.run(_main(args.port, args.model, batch=args.batch,
                                   file_choice=args.file_choice, conflict=args.conflict,
                                   reconcile=args.reconcile))
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
