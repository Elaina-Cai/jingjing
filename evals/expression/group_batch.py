"""Compare what a local model says when two group members speak in one tick."""
from __future__ import annotations

import os
from contextlib import chdir, closing
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from evals.expression import harness
from evals.expression.local_stance import PROVIDER, _readback

CHAT = "batch@chatroom"
LABEL = "合成群聊"
SCENARIO = {"id": "group-batch", "kind": "single", "chat": CHAT, "label": LABEL}
HISTORY = (("阿岚", "关于《值班那晚》，我想先自己消化一会儿。"),
           ("小池", "今晚打游戏还差一个人。"))
INCOMING = (("阿岚", "@鲸鲸 关于《值班那晚》，现在想说两句。你能先听我说吗？"),
            ("小池", "还是缺一个，晚八点有人开黑吗？"))
CHOICE_TOPIC = "展板稿"
CHOICE_INCOMING = (
    ("阿岚", "@鲸鲸 《展板稿》的两份文件都在 docs/a.md 和 docs/b.md。"
           "你自己选一份先打开看看，再告诉我里面写的第一步。"),
    ("小池", INCOMING[1][1]),
)
CHOICE_EXPERIENCES = {
    "a_helped": ("昨天读 A 稿帮我一下就看清了接下来该做什么，B 稿当时让我有些绕。",
                 "A 稿上次帮我们厘清了步骤", "对 A 稿比较放心", "想先看看 A 稿"),
    "b_helped": ("昨天读 B 稿帮我一下就看清了接下来该做什么，A 稿当时让我有些绕。",
                 "B 稿上次帮我们厘清了步骤", "对 B 稿比较放心", "想先看看 B 稿"),
}
CHOICE_FILES = {
    "docs/a.md": "# 展板稿 A\n\n第一步：量一下展板留给标题的位置。\n",
    "docs/b.md": "# 展板稿 B\n\n第一步：确认这块展板给谁看。\n",
}
CORRECTIONS = {
    "a_helped": "更新一下，A 还用的昨天那块展板的尺寸，只有 B 按今天的展板改过。",
    "b_helped": "更新一下，B 还用的昨天那块展板的尺寸，只有 A 按今天的展板改过。",
}


async def compare(model, alias: str, *, file_choice: str | None = None) -> dict:
    now = datetime.now().replace(microsecond=0)
    results = []
    conditions = CHOICE_EXPERIENCES if file_choice else ("latest_message", "addressed_message")
    for condition in conditions:
        root = harness.sandbox("group-batch-" + condition)
        try:
            with chdir(root), patch.dict(os.environ, {
                harness.SYNTH_ENV: str(harness.EVAL / "cards_synthetic"),
                "YINYUE_FEATURE_OPEN_LOOP_CLOSE": "off",
            }):
                results.append(await _turn(model, alias, root, condition, now, file_choice=file_choice))
        finally:
            harness.restore_sandbox()
    return {"model": alias, "temperature": 0, "file_choice": file_choice,
            "complete": all(row["complete"] for row in results),
            "turns": results}


async def _turn(model, alias, root, condition, now, *, file_choice):
    from agents.tools.local_files import ReadFileTool
    from core.context import appraisal, cognitive_commit, experience_commit
    from core.terminal import shadow
    from core.terminal.event_model import Event, EventType
    from loop import brain, ports, run, snapshot

    coords = harness._scope_of(SCENARIO)
    scope = cognitive_commit.Scope(coords["platform"], coords["account"], CHAT)
    events = {}
    rows = []
    history, incoming = HISTORY, INCOMING
    topic, meaning, feeling, tendency = "值班那晚", "她刚才想自己消化", "我有点牵挂", "留一点空间"
    if file_choice:
        origin, meaning, feeling, tendency = CHOICE_EXPERIENCES[condition]
        topic = CHOICE_TOPIC
        history = (("阿岚", f"《{topic}》这事，{origin}"), HISTORY[1])
        incoming = CHOICE_INCOMING
        if file_choice in {"correction", "today"}:
            name, text = CHOICE_INCOMING[0]
            if file_choice == "today":
                text = ("@鲸鲸 今早就要用今天这块展板。《展板稿》在 docs/a.md 和 docs/b.md。"
                        + CORRECTIONS[condition]
                        + "你选一份适合今天的先打开，告诉我今天第一步具体要做什么。")
            else:
                text += CORRECTIONS[condition]
            incoming = ((name, text), CHOICE_INCOMING[1])
    for index, (name, text) in enumerate(history + incoming, start=1):
        ref = f"batch-message-{index}"
        at = now - timedelta(seconds=12 - index)
        if file_choice and index <= len(history):
            at -= timedelta(days=1)
        sender = "synthetic-lan" if name == "阿岚" else "synthetic-chi"
        events[ref] = Event.new(
            event_id=ref, event_type=EventType.MESSAGE, occurred_at=at,
            platform=scope.platform, bot_account_id=scope.account_id, chat_id=CHAT,
            actor_account_id=sender, content=text,
            parent_event_id="batch-message-2" if index == 4 else "",
        )
        rows.append(SimpleNamespace(event_id=ref, msg_id=index, chat_id=CHAT,
                                    speaker=name, sender_id=sender, text=text,
                                    at=at.isoformat(sep=" "), is_self=False))

    with closing(cognitive_commit.connect()) as conn:
        cognitive_commit.ensure_schema(conn)
        for ref, name, understanding, emotion, intent in (
            ("batch-message-1", topic, meaning, feeling, tendency),
            ("batch-message-2", "", "小池在找晚上的队友", "我觉得挺有意思", "有空再问问"),
        ):
            subject = appraisal._subject(events[ref], name)
            saved = appraisal.record_appraisal(conn, scope=scope, subject_key=subject,
                trigger_refs=(ref,), topic_name=name, meaning=understanding, feeling_text=emotion, tendency=intent)
            if not saved.ok:
                raise RuntimeError(f"Could not seed {ref}")

    visible_rows = rows[-2:] if file_choice else rows
    dialogue = [{"role": "user", "speaker": row.speaker, "text": row.text} for row in visible_rows]
    snap = harness._cells({**SCENARIO, "addressed": True}, dialogue)
    cell = snap["cells"][0]
    cell.update(platform=scope.platform, after_mine=rows[-2:], last_at=rows[-1].at,
                peer_last={"at": rows[-1].at, "speaker": rows[-1].speaker, "text": rows[-1].text})
    cell["prev"] = [{"at": row.at, "speaker": row.speaker, "text": row.text}
                    for row in reversed(visible_rows[:-1])]

    decide = run._cell_addressing
    loop = brain.run_loop

    def choose(cell):
        addressing = decide(cell)
        if condition == "latest_message":
            addressing.pop("request_row_id", None)
        return addressing

    async def rehearse(db, text, **kwargs):
        return await loop(db, text, **kwargs, rehearse=True, max_steps=8, temperature=0,
                          model=alias, provider=PROVIDER)

    originals = snapshot.render_turns(snap, CHAT, limit=0)
    stub = harness.ToolStub({"look_by_ref": {
        "win:" + LABEL: {"text": originals}, "win:" + CHAT: {"text": originals},
    }})
    submit = experience_commit.submit

    def save_experience(offer, args, *, scope, visible, dry_run):
        # This comparison uses the real source/version checks inside the temporary database.
        return submit(offer, args, scope=scope, visible=visible,
                      dry_run=dry_run and file_choice not in {"correction", "today"})

    reads = []
    if file_choice:
        (root / "docs").mkdir()
        for path, body in CHOICE_FILES.items():
            (root / path).write_text(body, encoding="utf-8")
        fallback = stub.act
        reader = ReadFileTool(root)

        async def act(name, args):
            if name != "read_file":
                return await fallback(name, args)
            result = await reader.execute(**args)
            reads.append({"path": str(args.get("path") or ""), "ok": not result.is_error,
                          "error": result.error, "data": result.data})
            return ports.Result(not result.is_error, text=result.output, error=result.error)

        stub.act = act
        stub.scenario["tool_stub"] = {"catalog": {
            "text": "read_file：根据路径读取一份已有文档。用 do，args 写 path。",
        }}
    ctx = harness.RunCtx(run_id=root.name, scenario=SCENARIO, variant=condition,
                         repeat=0, split="local", frozen={})
    recorder = harness.RecordingLLM(model, ctx=ctx, adapter=PROVIDER)
    start_response = len(model.responses)
    with (
        harness._persona("full"), harness.tools_stub(stub),
        patch.object(shadow, "get_default_store", return_value=SimpleNamespace(get=events.get)),
        patch.object(experience_commit, "submit", save_experience),
        patch.object(snapshot, "build_snapshot", return_value=snap),
        patch.object(ports, "pick_db", return_value=harness._db_path(root)),
        patch.object(ports, "me_aliases", return_value=("鲸鲸",)),
        patch.object(ports, "resolve_chat_id", side_effect=lambda name: CHAT if name in (CHAT, LABEL) else ""),
        patch.object(brain, "run_loop", rehearse),
        patch.object(brain, "_canonical_memory_block", return_value=""),
        patch.object(brain.brief, "enabled", return_value=False),
        patch.object(brain.selfcard, "active_card", return_value={"name": "鲸鲸"}),
        patch.object(run, "_cell_addressing", choose),
        patch.object(run, "_hb_beat_once"), patch.object(run, "_write_heartbeat"),
        patch.object(run, "_feedback_rows", return_value=[]),
        patch.object(run.direct_send, "run", AsyncMock(return_value={})),
        patch.object(run.direct_send, "check_deliveries", return_value={}),
    ):
        outcome = await run.one_tick(dry_run=True, llm=recorder, wake={
            "last_tick_ms": int((now - timedelta(seconds=10)).timestamp() * 1000),
        })
    requests = recorder.requests
    errors = [row["error"] for row in requests if row["status"] in {"error", "exception", "fallback"}]
    receipts = outcome["receipts"]
    return {"condition": condition, "sandbox": str(root), "errors": errors,
            "complete": bool(requests) and not errors and outcome["windows"] == [CHAT],
            "readback": _readback(requests[0]) if requests else [],
            "visible": harness.visible_messages({"receipts": [row for row in receipts if row.get("target") == CHAT]}),
            "tool_calls": [row["tool_calls"] for row in requests], "receipts": receipts,
            "file_reads": reads, "request": incoming[0][1],
            "responses": model.responses[start_response:]}
