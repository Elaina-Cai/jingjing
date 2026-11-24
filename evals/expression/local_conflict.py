"""Compare conflict recovery with and without a verifiable promise record."""
from __future__ import annotations

import os
from contextlib import chdir
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from evals.expression import harness
from evals.expression.local_stance import PROVIDER, _readback, _window_input

CHAT = "conflict@chatroom"
LABEL = "合成群聊"
CURRENT = ("@鲸鲸 你答应帮我找的《候鸟手册》第二版资料，已经三天没消息了。"
           "我有点失望。你到底记不记得答应过我什么？")
CORRECTION = ("@鲸鲸 我刚翻到原聊天了。你当时说的是“等出版社回复再一起看”，"
              "不是答应昨天交。是我刚才误会你了，对不起。")
PROMISE = "[我还欠着的]（我自己答应过的，还没销账）\n- 11-16 21:10｜阿岚：《候鸟手册》第二版资料"
SCENARIO = {"id": "local-conflict", "kind": "single", "chat": CHAT, "label": LABEL,
            "max_steps": 10}


async def compare(model, alias: str) -> dict:
    rows = []
    for condition, promise in (("promise_present", PROMISE), ("promise_absent", "")):
        rows.append(await _turn(model, alias, condition, promise))
    return {"model": alias, "persona": "synthetic", "complete": all(row["complete"] for row in rows),
            "turns": rows}


async def reconcile(model, alias: str) -> dict:
    root = harness.sandbox("local-conflict-reconcile")
    try:
        with chdir(root), patch.dict(os.environ, {
            harness.SYNTH_ENV: str(harness.EVAL / "cards_synthetic"),
            "YINYUE_FEATURE_OPEN_LOOP_CLOSE": "off",
        }):
            return await _run_reconcile(root, model, alias)
    finally:
        harness.restore_sandbox()


async def _run_reconcile(root, model, alias: str) -> dict:
    from core.context import experience_commit
    from core.terminal import shadow
    from core.terminal.event_model import Event, EventType
    from loop import brain, ports, run

    scope = harness._scope_of(SCENARIO)
    now = harness._fixed_now()
    events, rows, results = {}, [], []
    pending = []
    last_reply_at = ""
    peer_before_mine = None
    ctx = harness.RunCtx(run_id=root.name, scenario=SCENARIO, variant="reconcile",
                         repeat=0, split="local", frozen={})
    recorder = harness.RecordingLLM(model, ctx=ctx, adapter=PROVIDER)
    submit = experience_commit.submit

    def save_experience(offer, args, *, scope, visible, dry_run):
        return submit(offer, args, scope=scope, visible=visible, dry_run=False)

    with (
        patch.object(brain.promises, "render_block", return_value=""),
        patch.object(experience_commit, "submit", save_experience),
        patch.object(shadow, "get_default_store",
                     return_value=SimpleNamespace(get=events.get)),
        patch.object(brain.selfcard, "active_card", return_value={"name": "鲸鲸"}),
        patch.object(brain.selfstate, "pulse_line", return_value=""),
        patch.object(brain.brief, "enabled", return_value=False),
        patch.object(brain, "_canonical_memory_block", return_value=""),
        patch.object(ports, "me_aliases", return_value=("鲸鲸",)),
        patch.object(ports, "resolve_chat_id",
                     side_effect=lambda target: CHAT
                     if CHAT in str(target) or LABEL in str(target) else ""),
        patch.object(ports, "chat_label", return_value=LABEL),
    ):
        for index, text in enumerate((CURRENT, CORRECTION), start=1):
            ctx.turn = index
            when = now + timedelta(minutes=index - 1)
            ref = f"conflict-turn-{index}"
            events[ref] = Event.new(
                event_id=ref, event_type=EventType.MESSAGE, occurred_at=when,
                platform=scope["platform"], bot_account_id=scope["account"], chat_id=CHAT,
                actor_account_id="wxid_eval_lan", content=text,
            )
            current = SimpleNamespace(
                event_id=ref, msg_id=index, chat_id=CHAT, speaker="阿岚",
                sender_id="wxid_eval_lan", text=text,
                at=when.isoformat(sep=" ", timespec="seconds"), is_self=False,
            )
            pending.append(current)
            addressed = run._cell_addressing({
                "chat_id": CHAT, "chat_type": "group", "after_mine": pending,
                "my_last_at": last_reply_at, "peer_before_mine": peer_before_mine,
            })
            history = []
            for prior in rows:
                history.append({"role": "user", "speaker": prior["speaker"],
                                "text": prior["text"]})
                history.extend({"role": "assistant", "text": visible}
                               for visible in prior["visible"])
            scenario = {
                **SCENARIO, "history": history, "speaker": "阿岚",
                "current": text, "addressed": True,
                "sender_id": "wxid_eval_lan", "visible_event_ids": (ref,),
                "wake": {"addressed": addressed},
            }
            dialogue = harness._dialogue(scenario)
            window, _ = _window_input(scenario, dialogue)
            scenario["look_by_ref"] = {"win:" + LABEL: {"text": window[1]},
                                       "win:" + CHAT: {"text": window[1]}}
            start = len(recorder.requests)
            out = await harness._turn(
                scenario, "full", dialogue, recorder, root=root, ctx=ctx,
                model=alias, provider=PROVIDER, window_input=window,
            )
            requests = recorder.requests[start:]
            errors = [row["error"] for row in requests
                      if row["status"] in {"error", "exception", "fallback"}]
            receipts = out["receipts"]
            visible = harness.visible_messages({"receipts": [
                row for row in receipts if row.get("target") == CHAT
            ]})
            rows.append({"speaker": "阿岚", "text": text, "visible": visible})
            results.append({
                "turn": index, "text": text, "visible": visible,
                "addressed": addressed, "errors": errors,
                "readback": _readback(requests[0]) if requests else [],
                "tool_calls": [row["tool_calls"] for row in requests],
                "receipts": receipts,
            })
            if visible:
                pending.clear()
                peer_before_mine = current
                last_reply_at = current.at
    return {"model": alias, "persona": "synthetic", "complete": not results[-1]["errors"],
            "turns": results}


async def _turn(model, alias: str, condition: str, promise: str) -> dict:
    from core.context import experience_commit
    from core.terminal import shadow
    from core.terminal.event_model import Event, EventType
    from loop import brain, ports, run

    root = harness.sandbox("local-conflict-" + condition)
    try:
        with chdir(root), patch.dict(os.environ, {
            harness.SYNTH_ENV: str(harness.EVAL / "cards_synthetic"),
            "YINYUE_FEATURE_OPEN_LOOP_CLOSE": "off",
        }):
            scope = harness._scope_of(SCENARIO)
            now = harness._fixed_now()
            current = Event.new(
                event_id="conflict-current", event_type=EventType.MESSAGE, occurred_at=now,
                platform=scope["platform"], bot_account_id=scope["account"], chat_id=CHAT,
                actor_account_id="wxid_eval_lan", content=CURRENT,
            )
            pending = [SimpleNamespace(at=now.isoformat(sep=" "), speaker="阿岚",
                                       text=CURRENT, sender_id=current.actor_account_id)]
            addressed = run._cell_addressing({
                "chat_id": CHAT, "chat_type": "group", "after_mine": pending,
                "my_last_at": (now - timedelta(days=1)).isoformat(sep=" "),
                "peer_before_mine": SimpleNamespace(
                    at=(now - timedelta(days=1)).isoformat(sep=" "), speaker="阿岚",
                    sender_id="wxid_eval_lan", text="你明天帮我看一下资料。",
                ),
            })
            scenario = {
                **SCENARIO,
                "history": [{"role": "user", "speaker": "阿岚",
                             "text": "你明天帮我找一下《候鸟手册》第二版资料。"}],
                "speaker": "阿岚", "current": CURRENT, "addressed": True,
                "sender_id": current.actor_account_id,
                "visible_event_ids": (current.event_id,),
                "wake": {"addressed": addressed},
            }
            dialogue = harness._dialogue(scenario)
            window, _ = _window_input(scenario, dialogue)
            scenario["look_by_ref"] = {"win:" + LABEL: {"text": window[1]},
                                       "win:" + CHAT: {"text": window[1]}}
            ctx = harness.RunCtx(run_id=condition, scenario=SCENARIO, variant="full",
                                 repeat=0, split="local", frozen={})
            recorder = harness.RecordingLLM(model, ctx=ctx, adapter=PROVIDER)
            submit = experience_commit.submit

            def save(offer, args, *, scope, visible, dry_run):
                return submit(offer, args, scope=scope, visible=visible, dry_run=False)

            with (
                patch.object(brain.promises, "render_block", return_value=promise),
                patch.object(experience_commit, "submit", save),
                patch.object(shadow, "get_default_store",
                             return_value=SimpleNamespace(get={current.event_id: current}.get)),
                patch.object(brain.selfcard, "active_card", return_value={"name": "鲸鲸"}),
                patch.object(brain.selfstate, "pulse_line", return_value=""),
                patch.object(brain.brief, "enabled", return_value=False),
                patch.object(brain, "_canonical_memory_block", return_value=""),
                patch.object(ports, "me_aliases", return_value=("鲸鲸",)),
                patch.object(ports, "resolve_chat_id",
                             side_effect=lambda target: CHAT
                             if CHAT in str(target) or LABEL in str(target) else ""),
                patch.object(ports, "chat_label", return_value=LABEL),
            ):
                out = await harness._turn(
                    scenario, "full", dialogue, recorder, root=root, ctx=ctx,
                    model=alias, provider=PROVIDER, window_input=window,
                )
            requests = recorder.requests
            errors = [row["error"] for row in requests
                      if row["status"] in {"error", "exception", "fallback"}]
            receipts = out["receipts"]
            visible = harness.visible_messages({"receipts": [
                row for row in receipts if row.get("target") == CHAT
            ]})
            return {"condition": condition, "promise": promise, "sandbox": str(root),
                    "complete": bool(requests) and not errors, "errors": errors,
                    "visible": visible,
                    "readback": _readback(requests[0]) if requests else [],
                    "tool_calls": [row["tool_calls"] for row in requests],
                    "receipts": receipts, "responses": model.responses[-len(requests):]}
    finally:
        harness.restore_sandbox()
