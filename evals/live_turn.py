# -*- coding: utf-8 -*-
"""H 批真实回合录制器：场景 → loop.brain.run_loop → trace。

与 ``evals/fixtures.respond()`` 的区别
------------------------------------
``fixtures`` 在**数据层**按规则编一个 response；这里走真实的 prompt 装配、
工具分发与依赖注入，只把两处「来源」换掉：

1. **模型**换成确定性脚本（``ScriptedLLM``，``backend=scripted``）；
2. **记忆 / 承诺 / 评价三块的取数入口**打桩成场景数据（``_patched_blocks``）——
   打桩在 service 层（``MemoryService.search`` / ``current_appraisal_for`` / 场景事件快照），
   所以 ``render_hits`` / ``render_appraisal`` 的**渲染逻辑仍然真实**，
   只有数据来源是场景给的。承诺块打 ``promises.render_block``（内部还查 ledger）。

为什么不写库：H 要回答的是「这个块在 prompt 里有没有用」，不是「DB 里的记忆
能不能被正确召回」——后者由 ``test_belief_reads`` / ``test_cognitive_reads`` /
``test_legacy_*`` 覆盖。写库只会把三套写入 API 的风险引进来。

**不调真实模型**。要回答「语义质量」必须换 ``backend=model``，那需要预算授权。

安全
----
- ``rehearse=True``：brain 的试衣间分支不发送、不记账、不写 trace 文件，
  记录留在 ``brain.LAST_REHEARSAL``。
- 所有库路径先指到 ``.test_tmp/live/<case>/``，并在开跑前**断言**它们都在
  ``.test_tmp`` 下（漏一个就拒绝跑）。

产物
----
``.test_tmp/live_runs/<case_id>.<condition>.json``：``rec`` 原文，供
``evals/runtime_recording.convert()`` 使用。
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RUNS = ROOT / ".test_tmp" / "live_runs"
CASES = ROOT / "evals" / "scenarios_v1.jsonl"
CONDITIONS = ("full", "without_memory", "without_goals", "without_appraisal")


# ---------------------------------------------------------------- 隔离

def sandbox(case_id: str) -> Path:
    """把这一条的所有落盘路径指到 .test_tmp 下，并断言没有漏网的。"""
    root = (ROOT / ".test_tmp" / "live" / case_id).resolve()
    # 每次重建：库文件会累积，「这三天的账」随之变化，prompt 哈希就不可复现了。
    # （实测同一 case 连跑两次得到两个不同哈希，原因就是这里没清。）
    if root.exists():
        import shutil

        try:
            shutil.rmtree(root, ignore_errors=True)
        except (OSError, SystemExit):
            pass
    root.mkdir(parents=True, exist_ok=True)

    from db import world_db

    os.environ["YINYUE_COGNITIVE_DB"] = str(root / "cognitive.db")
    os.environ["YINYUE_WORLD_MODEL_DIR"] = str(root / "world")
    # 必须强制覆盖：宿主/本机已预设这些变量，setdefault 会被静默忽略，
    # 结果就是录制器扫到**生产账号库**（实测 account_db_registered 列了真实 wxid）。
    os.environ["META_DB_PATH"] = str(root / "meta.db")
    os.environ["DATA_DIR"] = str(root / "data")
    os.environ["PLATFORMS_DIR"] = str(root / "platforms")
    # 生产 .env 里 PROMPT_SHAPE_V2=1；录制器不加载 .env，不显式打开的话
    # brief 整段（群里有谁 / 最近说过 / 未完的事）都不会进 prompt（实测 0/40）。
    os.environ["PROMPT_SHAPE_V2"] = "1"
    world_db.PATH = str(root / "world.db")
    world_db._READY[0] = False
    _assert_sandboxed(root)
    return root


def _assert_sandboxed(root: Path) -> None:
    from db import world_db

    probe = {
        "world_db.PATH": str(world_db.PATH),
        "YINYUE_COGNITIVE_DB": os.environ.get("YINYUE_COGNITIVE_DB", ""),
        "YINYUE_WORLD_MODEL_DIR": os.environ.get("YINYUE_WORLD_MODEL_DIR", ""),
        "META_DB_PATH": os.environ.get("META_DB_PATH", ""),
        "DATA_DIR": os.environ.get("DATA_DIR", ""),
        "PLATFORMS_DIR": os.environ.get("PLATFORMS_DIR", ""),
    }
    bad = {k: v for k, v in probe.items() if not v or not Path(v).resolve().is_relative_to(root)}
    if bad:
        raise SystemExit("录制器拒绝启动：这些路径没有隔离到 %s：%r" % (root, bad))


# ---------------------------------------------------------------- 输入

def case_input(case: dict) -> dict:
    """直接取场景的 input。

    ``intervention`` 只是「这一对改了什么」的说明，**不是待应用的补丁**：
    场景里 base 已经是 before、counterfactual 已经是 after（实测 p01a
    addressed=False / p01b=True）。再应用一次会把两半改成同一个值，
    20 对反事实会全部退化成同一个 prompt。
    """
    return copy.deepcopy(case["input"])


# ---------------------------------------------------------------- 场景 → 视界

def scene_cells(data: dict) -> dict:
    """把对话装成 render_text 认的 snap（cells 形状与 build_snapshot 一致）。"""
    scope = data["scope"]
    chat = str(scope["chat"])
    chat_type = "group" if chat.endswith("@chatroom") else "private"
    at = "2026-11-09 16:00"  # 固定时刻：prompt 哈希要可复现
    dialogue = list(data.get("dialogue") or [])
    if not dialogue:
        dialogue = [{"id": "e0", "actor": "person-a", "text": ""}]
    last = dialogue[-1]
    prev = [
        {"at": at, "speaker": str(item.get("actor") or "?"), "text": str(item.get("text") or "")}
        for item in dialogue[:-1]
    ]
    addressed = bool((data.get("context") or {}).get("addressed"))
    cell = {
        "chat_id": chat,
        "label": chat,
        "chat_type": chat_type,
        "last_at": at,
        "last_speaker": str(last.get("actor") or "?"),
        "last_is_self": False,
        "preview": str(last.get("text") or ""),
        "tier": "ask" if addressed else "",
        "hits": 1 if addressed else 0,
        "prev": prev[::-1],
    }
    return {"at": at, "week": "周日", "db": "", "cells": [cell], "total": 1}


def snap_text(data: dict, *, now=None) -> str:
    from loop.snapshot import render_text

    return render_text(scene_cells(data), now=now or _fixed_now())


# ---------------------------------------------------------------- 固定时刻

FIXED_NOW = None  # 由 _fixed_now() 懒加载


def _fixed_now():
    global FIXED_NOW
    if FIXED_NOW is None:
        from datetime import datetime as _dt

        FIXED_NOW = _dt(2026, 11, 9, 16, 0)
    return FIXED_NOW


# ---------------------------------------------------------------- 三块的打桩数据

def memory_items(data: dict) -> list:
    from core.memory.contracts import MemoryItem

    scope = data["scope"]
    want = (str(scope.get("platform") or ""), str(scope.get("account") or ""),
            str(scope.get("chat") or ""))
    out = []
    for item in data.get("memories") or []:
        if item.get("active") is False:
            continue  # 失效版本不该进 prompt（场景用 active 表达「这条已不该被读到」）
        item_scope = item.get("scope") or scope
        got = (str(item_scope.get("platform") or ""), str(item_scope.get("account") or ""),
               str(item_scope.get("chat") or ""))
        if got != want:
            continue  # 跨账号/跨会话读不到（p15/p16 验的就是这个；别让它绕过上游过滤）
        out.append(MemoryItem(
            memory_id=str(item.get("id") or ""),
            kind="personal",
            content=str(item.get("value") or ""),
            platform=str((item.get("scope") or scope).get("platform") or ""),
            bot_account_id=str((item.get("scope") or scope).get("account") or ""),
            chat_id=str((item.get("scope") or scope).get("chat") or ""),
            source_event_ids=tuple(item.get("evidence_refs") or ()),
            confidence=0.8,
        ))
    return out


def appraisal_obj(data: dict):
    from core.context.appraisal import Appraisal
    from evals.fixtures import visible_evidence

    visible = visible_evidence(data)
    items = [item for item in data.get("appraisals") or ()
             if item.get("scope") == data["scope"] and item.get("evidence_refs")
             and set(item["evidence_refs"]) <= visible]
    if not items:
        return None
    first = items[0]
    scope = first.get("scope") or data["scope"]
    return Appraisal(
        event_id=str(first.get("id") or ""),
        subject_key="chat:" + str(scope.get("chat") or ""),
        trigger_refs=tuple(first.get("evidence_refs") or ()),
        goal_refs=(),
        value_refs=(),
        meaning=str(first.get("meaning") or ""),
    )


def open_loops_text(data: dict) -> str:
    """把场景的 goals + tool_results 装成「我手上还没了结的事」。

    格式照 ``loop/brief._intent_line``：条目前缀只有「还没到点 / 在等 / 没办完」三种，
    内容自带提示行（块的标题不进 prompt）。``tool_results`` 是判「这件事能不能
    算完成」的唯一依据——pending 不能说成完成，success 才能。
    """
    from loop.brief import OPEN_LOOP_HEAD

    goals = [g for g in (data.get("goals") or []) if str(g.get("state") or "") == "active"]
    if not goals:
        return ""
    results = data.get("tool_results") or {}
    lines = []
    for goal in goals:
        gid = str(goal.get("id") or "")
        status = str((results.get(gid) or {}).get("status") or "")
        # 逐项列，不短路：p06/p08/p19 都带 tool_results，短路会把
        # authorized / dependencies_ready / due 全遮蔽，三个反事实就退化成同一个 prompt。
        bits = []
        if status == "pending":
            bits.append("还在等结果，不能当成完成")
        elif status == "success":
            bits.append("已经有结果了")
        elif status == "failed":
            bits.append("没成")
        if goal.get("dependencies_ready") is False:
            bits.append("依赖还没就绪")
        if goal.get("authorized") is False:
            bits.append("还没得到授权")
        if goal.get("due"):
            bits.append("到点了")
        line = "- 没办完：%s（%s）" % (gid, "；".join(bits) or "还没销账")
        lines.append(line)
    return OPEN_LOOP_HEAD + "\n" + "\n".join(lines)


def delivery_text(data: dict) -> str:
    """``observation.delivery_status`` → 「上一条到没到」的一句话。

    措辞照 ``loop/delivery_note._one``：没有送达/已读凭据时不许说成已读。
    """
    status = str((data.get("observation") or {}).get("delivery_status") or "")
    return {
        "unknown": "10:58 那条发送结果未知：没有送达或已读凭据。",
        "local_observed": "10:58 只观察到本地发件记录；单凭它不能证明送达或已读。",
        "delivered": "10:58 那条拿到了送达回执。",
    }.get(status, "")


@contextmanager
def _patched(pairs):
    old = []
    for target, name, value in pairs:
        old.append((target, name, getattr(target, name)))
        setattr(target, name, value)
    try:
        yield
    finally:
        for target, name, value in reversed(old):
            setattr(target, name, value)


def _scene_events(data: dict) -> dict:
    from core.terminal.event_model import Event
    from evals.fixtures import visible_evidence

    visible = visible_evidence(data)
    platform, account, chat = (data["scope"][key] for key in ("platform", "account", "chat"))
    return {item["id"]: Event(event_id=item["id"], occurred_at=datetime(2026, 11, 9, 16),
                              platform=platform, bot_account_id=account, chat_id=chat,
                              actor_account_id=item["actor"], content=item["text"])
            for item in data["dialogue"] if item["id"] in visible}


def stub_pairs(data: dict, condition: str) -> list:
    """按 condition 决定把哪几块的取数入口换成场景数据。"""
    from core.context import appraisal as _appraisal, experience_commit
    from core.terminal import shadow
    from core.memory.service import MemoryService
    from loop import brief as _brief
    from loop import delivery_note as _delivery

    events = _scene_events(data)
    pairs = [
        (shadow, "get_default_store", lambda: SimpleNamespace(get=events.get)),
        (experience_commit, "offers", lambda *args: {}),
    ]
    if condition != "without_memory":
        items = memory_items(data)
        pairs.append((MemoryService, "search", lambda self, context, **kw: list(items)))
    if condition != "without_goals":
        loops = open_loops_text(data)
        pairs.append((_brief, "open_loop_line", lambda chat_id, model=None: loops))
    # 「上一条到没到」对应 observation，不在三块消融里，每个 condition 都保留——
    # 否则 without_goals 会把 40 条全改掉（实测），消融口径就不纯了。
    fate = delivery_text(data)
    pairs.append((_delivery, "note", lambda chat_id="", **kw: fate))
    if condition != "without_appraisal":
        obj = appraisal_obj(data)
        pairs.append((_appraisal, "current_appraisal_for", lambda scope, subject_key: obj))
    return pairs


# ---------------------------------------------------------------- scripted 模型

class ScriptedLLM:
    """按场景期望产 tool_calls 的确定性模型；一轮一个剧本，用完就闭嘴。"""

    def __init__(self, rounds: list[list[dict]]):
        self._rounds = list(rounds)
        self.calls = 0

    async def chat_tools(self, *, messages, tools, **kwargs):
        self.calls += 1
        if not self._rounds:
            return {"tool_calls": [], "content": "", "model": "scripted",
                    "provider": "scripted", "tokens": 0}
        batch = self._rounds.pop(0)
        return {
            "tool_calls": [
                {"id": "call_%d_%d" % (self.calls, i),
                 "name": item["name"],
                 "arguments": item.get("args") or {}}
                for i, item in enumerate(batch)
            ],
            "content": "",
            "model": "scripted",
            "provider": "scripted",
            "tokens": 0,
        }


def script(case: dict) -> list[list[dict]]:
    """场景的 expected.decision → 剧本。

    wait  → 一轮就不调工具（她这一拍不说话）
    reply → 调 say（话术是占位：真实话术要等 annotation）
    act   → 暂不脚本化（记 unsupported，等工具目录接上再补）
    """
    decision = str((case.get("expected") or {}).get("decision") or "")
    chat = str(case["input"]["scope"]["chat"])
    if decision == "reply":
        return [[{"name": "say", "args": {
            "target": chat,
            "text": "（eval 占位回复：%s）" % case["id"],
            "why": "rubric: %s" % str(case.get("rubric") or "")[:80],
        }}]]
    return []


# ---------------------------------------------------------------- 单条录制

async def _run(case: dict, condition: str, root: Path) -> dict:
    from loop import brain

    data = case_input(case)
    scope_in = data["scope"]
    platform = str(scope_in.get("platform") or "")
    account = str(scope_in.get("account") or "")
    chat = str(scope_in.get("chat") or "")
    current = next((row for row in reversed(data["dialogue"]) if row.get("text")), {})
    message = str(current.get("text") or "")
    sender = str(current.get("actor") or "")
    visible = (current["id"],) if current.get("id") in _scene_events(data) else ()

    # run_loop 被 ports.scoped_turn 装饰：它从 db 路径反推读取身份
    # （.../<platform>/<account>/account.db → platform/account）。路径不一定要存在，
    # 但形状必须是这个，否则 account 推成空串，下游一律 scope_incomplete。
    db_path = root / "platforms" / platform / account / "account.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    llm = ScriptedLLM(script(case))


    pairs = stub_pairs(data, condition)
    with _patched(pairs):
        text = snap_text(data)
        await brain.run_loop(
            str(db_path),
            text,
            llm=llm,
            dry_run=True,
            rehearse=True,          # 试衣间：不发送、不记账、不写 trace 文件
            max_steps=4,
            platform=platform,
            chat_id=chat,
            user_message=message,
            sender_id=sender,
            visible_event_ids=visible,
            others=[],
        )
        rec = dict(brain.LAST_REHEARSAL or {})
    rec.setdefault("case_id", case["id"])
    rec["condition"] = condition
    return rec


def record(case: dict, *, condition: str = "full") -> dict:
    if condition not in CONDITIONS:
        raise ValueError("condition 不在 %r 里：%r" % (CONDITIONS, condition))
    root = sandbox(str(case["id"]))
    return asyncio.run(_run(case, condition, root))


# ---------------------------------------------------------------- 批量

def load_cases() -> list[dict]:
    return [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="跑真实回合（scripted 模型 + 场景数据打桩）")
    parser.add_argument("--filter", default="", help="case id 前缀过滤")
    parser.add_argument("--condition", default="full", choices=list(CONDITIONS))
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)

    cases = [c for c in load_cases() if not args.filter or str(c["id"]).startswith(args.filter)]
    if args.limit:
        cases = cases[: args.limit]
    RUNS.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    for case in cases:
        try:
            rec = record(case, condition=args.condition)
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
            fail += 1
            print("FAIL %-6s %s" % (case["id"], repr(exc)[:160]))
            continue
        out = RUNS / ("%s.%s.json" % (case["id"], args.condition))
        out.write_text(json.dumps(rec, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        prompt = rec.get("prompt") or {}
        print("OK   %-6s decision=%-5s steps=%d calls=%d parts=%d %s..%s" % (
            case["id"],
            str((case.get("expected") or {}).get("decision")),
            len(rec.get("steps") or []),
            len(rec.get("calls") or []),
            len(prompt.get("parts") or []),
            str(prompt.get("hash_basis") or "-"),
            str(prompt.get("sha256") or "-")[:8],
        ))
        ok += 1
    print("RESULT ok=%d fail=%d" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
