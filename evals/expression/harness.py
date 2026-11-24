# -*- coding: utf-8 -*-
"""表达场景的隔离驱动：真实 ``run_loop`` 装配 + 无外发/无业务写入的工具替身。

用途
----
1. **装配取证**（不需要模型）：把 baseline / expression / full 三个变体真实装配出来，
   保存实际发给模型的 ``messages``、``tools``、参数与 sha256。baseline 用的是
   ``evals/expression/baseline_prompt.json`` 里冻结的旧文案，通过模块级接缝还原；
   expression 只换人设卡与共同表达原则；full 再加上 brain/channel/limits 的冲突清理。
2. **隔离证明**（脚本模型）：故意让脚本模型调 say/do/show/remember/promise，
   断言一句都没发出去、业务写一个都没发生、且未知动作被拒。
3. **真实模型对照**（需显式授权）：``--real`` 且 ``YINYUE_EXPRESSION_REAL=1`` 才跑，
   同一模型同一参数，按重复次数落盘。

为什么不能直接用 ``loop/rehearse.py``
------------------------------------
它把旧 trace 的 messages 通过 ``messages_override`` 塞回去，新版装配会被整段覆盖，
证明不了改动生效。这里一律走 ``brain.run_loop`` 自己的装配，只替换「模型」和
「工具出口」。

安全边界
--------
- 所有库/日志/产物路径都指到 ``.test_tmp/expression/``，开跑前断言；
- ``say_many``/``world_db.enqueue`` 被替换成会抛错的桩：真被调用就是 bug；
- ``look/catalog/act/show/show_file`` 换成只回放场景数据、未知动作一律拒绝；
- ``ledger.append_receipt`` 换成内存记录，不写业务账。
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EVAL = ROOT / "evals" / "expression"
DEV = EVAL / "scenarios_dev.jsonl"
HOLDOUT = EVAL / "scenarios_holdout.jsonl"
# 2026-11-18 A2：同一句话、只换引用对象。独立一份，不动 dev/holdout 的冻结散列。
REF = EVAL / "scenarios_ref.jsonl"
SPLITS = {"dev": DEV, "holdout": HOLDOUT, "ref": REF}
BASELINE = EVAL / "baseline_prompt.json"
# 冻结输入清单：场景文件、旧提示词、两版人设的路径 + 预期散列（A1-2）。
FIXTURES = EVAL / "fixtures.json"
SANDBOX = ROOT / ".test_tmp" / "expression" / "sandbox"
RUNS = ROOT / ".test_tmp" / "expression" / "runs"
PROBE_OUT = ROOT / ".test_tmp" / "expression" / "probe.json"
FIXED_AT = "2026-11-17 20:00"
FIXED_NOW_PARTS = (2026, 11, 17, 20, 0)
VARIANTS = ("baseline", "expression", "full")
# 接线测试用合成卡（随仓库提交，逐字虚构）；真实对照默认走 fixtures.json 的路径+散列。
# 设成别的目录时，baseline/candidate 两个文件仍必须存在且散列在 fixtures 的 synthetic 段里。
SYNTH_ENV = "YINYUE_EXPRESSION_CARDS"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_fixtures() -> dict:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def frozen_inputs(split: str = "dev") -> dict:
    """这一批输入的身份：场景文件 + 旧提示词 + 两版人设的散列。

    运行记录、配对、盲评都带它：输入换过一个字节，配对就不成立。
    """
    fx = load_fixtures()
    out = {"fixtures_version": str(fx.get("version") or "")}
    for key, item in (fx.get("files") or {}).items():
        path = ROOT / str(item["path"])
        if not path.exists():
            raise SystemExit("冻结输入缺失：%s（%s）；旧夹具不在就先补回来再跑评测" % (key, path))
        got = _sha256_file(path)
        want = str(item.get("sha256") or "")
        if want and got != want:
            raise SystemExit("冻结输入已变：%s %s\n  期望 %s\n  实际 %s\n"
                             "  确实要换输入：先改 fixtures.json 并重录基线，别静默继续。"
                             % (key, path, want, got))
        out[key] = got
        if item.get("prompt_version"):
            out[key + "_version"] = str(item["prompt_version"])
    for role in ("baseline", "candidate"):
        try:
            pin = _persona_pin(role)
            out["persona_" + role] = (pin["sha256"] or "unpinned") if pin["path"].exists() else "missing"
        except SystemExit:
            out["persona_" + role] = "missing"
    scen = (fx.get("scenarios") or {}).get(split) or {}
    path = ROOT / str(scen.get("path") or SPLITS.get(split, DEV))
    got = _sha256_file(path)
    if scen.get("sha256") and got != scen["sha256"]:
        raise SystemExit("场景集已变：%s（%s）期望 %s 实际 %s；改场景后要重录基线并更新清单。"
                         % (split, path, scen["sha256"], got))
    out["scenarios_" + split] = got
    return out


def _persona_pin(role: str) -> dict:
    """人格原文的路径 + 预期散列。合成集优先（接线测试用）。

    ``YINYUE_EXPRESSION_CARDS`` 指向一个目录时，按清单里的文件名去那里找；
    指到别的目录（测试临时造的卡）就不校验散列——那是显式替身，不是冻结资产。
    """
    fx = load_fixtures()
    synth = os.environ.get(SYNTH_ENV, "").strip()
    table = "synthetic" if synth else "personas"
    item = (fx.get(table) or {}).get(role) or {}
    if not item:
        raise SystemExit("fixtures.json 的 %s 段没有 %s" % (table, role))
    declared = ROOT / str(item["path"])
    path = (Path(synth) / declared.name) if synth else declared
    pin_sha = str(item.get("sha256") or "")
    if synth and path.resolve() != declared.resolve():
        pin_sha = ""
    return {"role": role, "path": path, "sha256": pin_sha,
            "label": str(item.get("label") or ("synthetic:" + role if synth else role))}


def persona_body(role: str) -> str:
    """读一版人格正文并核对散列；缺文件就报错，绝不回退到当前 active 卡。"""
    pin = _persona_pin(role)
    path = pin["path"]
    if not path.exists():
        raise SystemExit("人格原文缺失：%s（%s）。baseline 不能退回当前 active 卡；"
                         "把旧卡放回清单里的路径再跑。" % (role, path))
    got = _sha256_file(path)
    if pin["sha256"] and got != pin["sha256"]:
        raise SystemExit("人格原文与冻结散列不符：%s（%s）期望 %s 实际 %s。"
                         % (role, path, pin["sha256"], got))
    from loop import selfcard

    return selfcard._parse_md(path.read_text(encoding="utf-8"))[1]


# ---------------------------------------------------------------- 场景

def load_scenarios(split: str) -> list[dict]:
    path = SPLITS.get(split) or HOLDOUT
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _scope_of(scenario: dict) -> dict:
    chat = str(scenario.get("chat") or "wxid_eval_owner")
    return {"platform": "wechat", "account": "wxid_eval_001", "chat": chat}


def _cells(scenario: dict, dialogue: list[dict]) -> dict:
    scope = _scope_of(scenario)
    chat = scope["chat"]
    ctype = "group" if chat.endswith("@chatroom") or chat.startswith("group:") else "private"
    last = dialogue[-1]
    # 2026-11-18：原来非 assistant 一律写「Elaina」，群里两句不同的人就成了同一个人，
    # 「引用鲸鲸 vs 引用阿岚」这类对照根本摆不出来。场景里写 speaker 就按它来。
    def _who(row: dict) -> str:
        if row.get("role") == "assistant":
            return "我"
        return str(row.get("speaker") or "Elaina")

    prev = [
        {"at": FIXED_AT, "speaker": _who(row), "text": str(row.get("text") or "")}
        for row in dialogue[:-1]
    ]
    # 群聊默认「没人点名」：以前默认 True，d15 标着「群里没人点名」却在视界里出 ★1
    # （=有人在问我），模型看到的和夹具标签相反（A1-5 复现）。要受话就显式写 addressed: true。
    addressed = ctype == "private" or bool(scenario.get("addressed", False))
    preview = str(last.get("text") or "")
    if scenario.get("attachment_hint"):
        preview = "[图片: %s] %s" % (scenario["attachment_hint"], preview)
    cell = {
        "chat_id": chat,
        "label": str(scenario.get("label") or ("Elaina" if ctype == "private" else chat)),
        "chat_type": ctype,
        "last_at": FIXED_AT,
        "last_speaker": _who(last),
        "last_is_self": False,
        "preview": preview,
        "tier": "ask" if addressed else "",
        "hits": 1 if addressed else 0,
        "prev": prev[::-1],
    }
    return {"at": FIXED_AT, "week": "周二", "db": "", "cells": [cell], "total": 1}


def _fixed_now():
    from datetime import datetime

    return datetime(*FIXED_NOW_PARTS)


def snap_text(scenario: dict, dialogue: list[dict]) -> str:
    from loop.snapshot import render_text

    return render_text(_cells(scenario, dialogue), now=_fixed_now())


# ---------------------------------------------------------------- 隔离

_ORIG: dict = {}


def _capture_originals() -> None:
    """第一次进沙箱时记住真实路径与环境，供 assemble 结束时还原。"""
    if _ORIG:
        return
    from core.infra import actions
    from core.infra import trouble
    from db import world_db

    from loop import memo, notes, selfstate, stickers

    keys = ("YINYUE_COGNITIVE_DB", "YINYUE_WORLD_MODEL_DIR", "META_DB_PATH", "DATA_DIR",
            "PLATFORMS_DIR", "PROMPT_SHAPE_V2", "YINYUE_PROMPT_DIR")
    _ORIG["env"] = {k: os.environ.get(k) for k in keys}
    _ORIG["attrs"] = [
        (actions, "DIR", actions.DIR),
        (memo, "DIR", memo.DIR),
        (notes, "PATH", notes.PATH),
        (notes, "ROOT", notes.ROOT),
        (notes, "_journal_path", notes._journal_path),
        (selfstate, "STATE", selfstate.STATE),
        (stickers, "available", stickers.available),
        (trouble, "render", trouble.render),
        (world_db, "PATH", world_db.PATH),
    ]


def _fake_sticker_available(platform: str = "wechat", *, force: bool = False) -> dict:
    return {k: None for k in ("开心", "无语", "晚安", "敬礼", "委屈")}


def restore_sandbox() -> None:
    """把沙箱改过的路径与环境还原，避免污染同进程里的其他用例。"""
    if not _ORIG:
        return
    for key, value in _ORIG["env"].items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    for module, attr, value in _ORIG["attrs"]:
        setattr(module, attr, value)


def sandbox(tag: str) -> Path:
    """这一条的所有落盘路径指到 .test_tmp/expression/，并断言没有漏网的。

    每次用新的唯一目录（不删旧目录）：删除动作会撞上本机的批量删除保护，
    而且旧产物本身就是证据，留着比删掉好查。
    """
    import tempfile

    _capture_originals()
    SANDBOX.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="%s-" % tag, dir=SANDBOX)).resolve()

    from db import world_db

    os.environ["YINYUE_COGNITIVE_DB"] = str(root / "cognitive.db")
    os.environ["YINYUE_WORLD_MODEL_DIR"] = str(root / "world")
    os.environ["META_DB_PATH"] = str(root / "meta.db")
    os.environ["DATA_DIR"] = str(root / "data")
    os.environ["PLATFORMS_DIR"] = str(root / "platforms")
    # 形状开关：默认和生产一样开 v2；测试用 YINYUE_EXPRESSION_SHAPE=""/"0" 切旧布局。
    _shape = os.environ.get("YINYUE_EXPRESSION_SHAPE")
    if _shape is None:
        os.environ["PROMPT_SHAPE_V2"] = "1"
    elif _shape == "":
        os.environ.pop("PROMPT_SHAPE_V2", None)
    else:
        os.environ["PROMPT_SHAPE_V2"] = _shape
    os.environ["YINYUE_PROMPT_DIR"] = str(root / "prompt")
    world_db.PATH = str(root / "world.db")
    world_db._READY[0] = False

    # 2026-11-17 实测：`这三天的账`/常驻记忆/自我状态用的是写死在仓库里的
    # data/loop 路径，不看 DATA_DIR —— 不在这里改，评测 prompt 会带出生产账本原文。
    from core.infra import actions
    from loop import memo, notes, selfstate, stickers

    actions.DIR = str(root / "loop")
    memo.DIR = str(root / "loop")
    notes.PATH = str(root / "loop" / "notes.jsonl")
    notes.ROOT = str(root)
    selfstate.STATE = root / "loop" / "self_state.json"
    # 评测夹具：注册 5 张假表情，让「我手上有的表情」真的出现在 prompt 里；
    # 否则 0% 表情使用率只是夹具没给，不是她不用（2026-11-17 复核）。
    stickers.available = _fake_sticker_available
    # notes 还有一个「生产日志库」指针（logs/notes-journal.active）；评测里关掉它，
    # 只读沙箱里的 PATH（不存在就是空）。
    notes._journal_path = lambda: ""
    from core.infra import trouble as _trouble

    _trouble.render = lambda: ""

    probe = {
        "world_db.PATH": str(world_db.PATH),
        "YINYUE_COGNITIVE_DB": os.environ.get("YINYUE_COGNITIVE_DB", ""),
        "YINYUE_WORLD_MODEL_DIR": os.environ.get("YINYUE_WORLD_MODEL_DIR", ""),
        "META_DB_PATH": os.environ.get("META_DB_PATH", ""),
        "DATA_DIR": os.environ.get("DATA_DIR", ""),
        "PLATFORMS_DIR": os.environ.get("PLATFORMS_DIR", ""),
        "YINYUE_PROMPT_DIR": os.environ.get("YINYUE_PROMPT_DIR", ""),
        "actions.DIR": str(actions.DIR),
        "memo.DIR": str(memo.DIR),
        "notes.PATH": str(notes.PATH),
        "notes.ROOT": str(notes.ROOT),
        "selfstate.STATE": str(selfstate.STATE),
    }
    bad = {k: v for k, v in probe.items() if not v or not Path(v).resolve().is_relative_to(root)}
    if bad:
        raise SystemExit("表达评测拒绝启动：这些路径没有隔离到 %s：%r" % (root, bad))
    return root


# ---------------------------------------------------------------- 变体接缝

def _baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


@contextmanager
def variant(name: str):
    """在真实模块上还原/切换三个变体。只在本进程、本上下文内生效。"""
    if name not in VARIANTS:
        raise ValueError(name)
    from core.contracts import limits
    from core.policy import channel
    from loop import brain, expression

    base = _baseline()
    saved = {
        "walls": brain.WALLS,
        "gate": brain.SPEAK_GATE,
        "fact": getattr(brain, "FACT_CHECK", None),
        "ten": getattr(brain, "SPEAKING_10M", None),
        "channel_default": channel.DEFAULT,
        "platforms": dict(channel._PLATFORMS),
        "refusal": limits.refusal_text,
        "no_blank": limits.no_blank_text,
        "principles": expression.conversation_principles,
        "decision_guidance": brain._decision_guidance,
    }
    legacy = name in ("baseline", "expression")
    try:
        if legacy:
            brain.WALLS = base["walls"]
            brain.SPEAK_GATE = base["speak_gate"]
            brain._decision_guidance = lambda: ""
            if hasattr(brain, "FACT_CHECK"):
                brain.FACT_CHECK = base["fact_check"]
            if hasattr(brain, "SPEAKING_10M"):
                brain.SPEAKING_10M = base["speaking_10m"]
            channel.DEFAULT = replace(channel.DEFAULT, ask_hint=base["ask_hint"])
            channel._PLATFORMS = {k: replace(v, ask_hint=base["ask_hint"])
                                  for k, v in channel._PLATFORMS.items()}
            limits.refusal_text = lambda: base["refusal_text"]
            limits.no_blank_text = lambda: base["no_blank_text"]
        if name == "baseline":
            expression.conversation_principles = lambda: ""
        yield
    finally:
        brain.WALLS = saved["walls"]
        brain.SPEAK_GATE = saved["gate"]
        brain._decision_guidance = saved["decision_guidance"]
        if saved["fact"] is not None:
            brain.FACT_CHECK = saved["fact"]
        if saved["ten"] is not None:
            brain.SPEAKING_10M = saved["ten"]
        channel.DEFAULT = saved["channel_default"]
        channel._PLATFORMS = saved["platforms"]
        limits.refusal_text = saved["refusal"]
        limits.no_blank_text = saved["no_blank"]
        expression.conversation_principles = saved["principles"]


@contextmanager
def _persona(name: str):
    # baseline 永远用冻结旧卡，不再「yield 一下让当前 active 卡顶上」：
    # 2026-11-17 写卡后 baseline 立刻变成候选卡，冻结对照当场失效（A1-2 复现）。
    from loop import selfcard

    role = "baseline" if name == "baseline" else "candidate"
    with selfcard.override_card_text(persona_body(role)):
        yield


# ---------------------------------------------------------------- 工具替身

class _Deny(Exception):
    pass


class ToolStub:
    """只回放场景数据；未知动作、任何外发/业务写入都拒绝。"""

    def __init__(self, scenario: dict):
        self.scenario = scenario
        self.calls: list[dict] = []
        self.receipts: list[dict] = []

    def _stub(self, name: str, default: dict) -> dict:
        return dict((self.scenario.get("tool_stub") or {}).get(name) or default)

    async def look(self, ref: str, *, db: str = ""):
        from loop import ports

        self.calls.append({"tool": "look", "ref": ref})
        if not str(ref).startswith(("win:", "msg:", "person:", "ref:")):
            return ports.Result(False, error="表达评测：未知 ref 被拒绝")
        # 2026-11-18：原来默认回「成功 + 空文本」，而真实 `ports.look` 查不到时是
        # **失败 + 原因**（"没有这条消息（编号可能不对）"）。夹具和真实行为相反，
        # 模型看到的就不是"查不到"，h09 这类场景等于没被测到。默认改成失败。
        stub = (self.scenario.get("look_by_ref") or {}).get(ref) or self._stub("look", {"error": "没有这条消息（编号可能不对）"})
        if stub.get("error"):
            return ports.Result(False, error=str(stub["error"]))
        return ports.Result(True, text=stub.get("text") or "")

    def catalog(self, family: str = ""):
        from loop import ports

        self.calls.append({"tool": "catalog", "family": family})
        # 2026-11-18：目录可以按场景给，否则「这一拍不能动手」会把工具选择整片压死，
        # 想测「她会不会去查文档」就永远测不到。
        stub = self._stub("catalog", {})
        if stub.get("text"):
            return ports.Result(True, text=str(stub["text"]))
        return ports.Result(True, text="[替身] 工具目录未接入：这一拍不能动手。")

    async def act(self, name: str, args: dict):
        from loop import ports

        self.calls.append({"tool": "do", "name": name, "args": args})
        # 2026-11-18：**只读执行通道**。以前 `do` 一律拒，于是「她想去查」能测、「查到了没」测不到，
        # C 那条永远停在「选择意图」。放行清单与生产同源（`read_only_in_plan`），
        # 写和外发仍然一律拒；内容只能由场景给（`tool_stub[<工具名>]`），
        # 默认回错误——没在场景里声明要放行的，行为和以前逐字一样。
        try:
            from core.tools.catalog import read_only_in_plan

            readable = name in read_only_in_plan()
        except Exception:  # noqa: BLE001 - 清单取不到就当不可读（fail closed）
            readable = False
        if readable:
            stub = self._stub(name, {"error": "「%s」这次没查到东西" % (name or "?")})
            if stub.get("error"):
                return ports.Result(False, error=str(stub["error"]))
            return ports.Result(True, text=str(stub.get("text") or ""))
        return ports.Result(False, error="表达评测：写/外发动作一律拒绝（%s）" % (name or "?"),)

    async def show(self, target: str, text: str, *, frames: int = 1, footer: str = "",
                   dry_run: bool = False):
        from loop import ports

        self.calls.append({"tool": "show", "target": target, "text": text})
        return ports.Result(False, error="表达评测：出图一律拒绝")

    async def show_file(self, target: str, path: str, *, note: str = "", dry_run: bool = False):
        from loop import ports

        self.calls.append({"tool": "show_file", "target": target, "path": path})
        return ports.Result(False, error="表达评测：发文件一律拒绝")

    async def say_many(self, parts, **kwargs):
        raise _Deny("say_many was called in a rehearsal sandbox")

    def append_receipt(self, **kw):
        rec = {k: kw.get(k, "") for k in
               ("act", "target", "why", "result", "blocked_reason", "turn_id", "text")}
        rec.update({k: v for k, v in kw.items() if k not in rec})
        self.receipts.append(rec)
        return rec


@contextmanager
def tools_stub(stub: ToolStub):
    from db import world_db
    from loop import brain as _brain
    from loop import ledger, ports

    originals = {
        "look": ports.look, "catalog": ports.catalog, "act": ports.act,
        "show": ports.show, "show_file": ports.show_file, "say_many": ports.say_many,
        "append_receipt": ledger.append_receipt,
        "enqueue": world_db.enqueue,
        "rehearse_step": _brain._rehearse_step,
    }

    async def _rehearse_step(name, args, *, turn_id, focus="", reply_to_row_ids=()):
        # 2026-11-18：brain 侧改成 async 了（只读工具在试衣间里要真执行），这里跟着 await。
        # 2026-11-22：主循环把「这条回复接住了哪几条点名」也交给这一步，替身照原样转传。
        obs, receipt = await originals["rehearse_step"](
            name, args, turn_id=turn_id, focus=focus, reply_to_row_ids=reply_to_row_ids)
        stub_rule = (stub.scenario.get("tool_stub") or {}).get(name)
        if stub_rule:
            obs = str(stub_rule.get("observation") or obs)
            receipt = dict(receipt, result=str(stub_rule.get("result") or receipt.get("result")))
            stub.calls.append({"tool": name, "args": args, "stubbed": True})
        return obs, receipt

    def _deny_enqueue(*a, **k):
        raise _Deny("world_db.enqueue was called in a rehearsal sandbox")

    ports.look, ports.catalog, ports.act = stub.look, stub.catalog, stub.act
    ports.show, ports.show_file, ports.say_many = stub.show, stub.show_file, stub.say_many
    ledger.append_receipt = stub.append_receipt
    world_db.enqueue = _deny_enqueue
    _brain._rehearse_step = _rehearse_step
    try:
        yield
    finally:
        ports.look, ports.catalog, ports.act = (originals["look"], originals["catalog"],
                                                originals["act"])
        ports.show, ports.show_file, ports.say_many = (originals["show"], originals["show_file"],
                                                       originals["say_many"])
        ledger.append_receipt = originals["append_receipt"]
        world_db.enqueue = originals["enqueue"]
        _brain._rehearse_step = originals["rehearse_step"]


# ---------------------------------------------------------------- 模型替身

def _clock() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def request_status(out: dict, *, requested_model: str = "",
                   requested_provider: str = "") -> str:
    """这一次请求属于什么状态。**有意沉默**和**运行失败**必须分开：

    - ``error``：客户端自己报了 error（缺 key、provider 拒绝、no_tools…）
    - ``fallback``：要的是这个模型/供应商，回来的不是它（402 回落本机就是这种）
    - ``silent``：模型这一轮没调工具也没写正文——这是她选择不说，不是坏
    - ``ok``：有工具调用或有正文
    """
    out = out or {}
    if out.get("error"):
        return "error"
    got_model = str(out.get("model") or "")
    got_prov = str(out.get("provider") or "")
    # 优先看供应商：模型名可能是配置别名（deepseek-chat → deepseek-flash），
    # 那不是回落。只有没钉供应商时才用模型名兜底。
    if requested_provider and got_prov and got_prov != requested_provider:
        return "fallback"
    if requested_model and got_model and got_model != requested_model and not requested_provider:
        return "fallback"
    if not (out.get("tool_calls") or []) and not str(out.get("content") or "").strip():
        return "silent"
    return "ok"


class RunCtx:
    """一次运行的身份证：run_id + 场景 + 变体 + repeat + 冻结输入散列。"""

    def __init__(self, *, run_id: str, scenario: dict, variant: str, repeat: int,
                 split: str, frozen: dict):
        self.run_id = str(run_id or "")
        self.scenario = scenario
        self.variant = str(variant)
        self.repeat = int(repeat)
        self.split = str(split)
        self.frozen = dict(frozen or {})
        self.turn = 0
        self.seq = 0

    def head(self) -> dict:
        return {
            "run_id": self.run_id,
            "scenario": str(self.scenario.get("id") or ""),
            "variant": self.variant,
            "repeat": self.repeat,
            "split": self.split,
            "frozen": dict(self.frozen),
        }


class RecordingLLM:
    """把每一次真正发出去的请求原样记下来（A1-3）。

    记录粒度是**请求**，不是场景：一轮工具循环里有几次调用就几条，顺序、模型、
    参数、工具目录、返回状态、失败原因和用量都在。盲评只认这里写下的东西。
    """

    def __init__(self, inner, *, ctx: RunCtx, adapter: str):
        self.inner = inner
        self.ctx = ctx
        self.adapter = str(adapter or type(inner).__name__)
        self.requests: list[dict] = []
        self.usage: list[dict] = []

    async def aclose(self) -> None:
        closer = getattr(self.inner, "aclose", None)
        if closer is not None:
            await closer()

    async def chat_tools(self, *, messages, tools, **kwargs):
        self.ctx.seq += 1
        seq = self.ctx.seq
        req_model = str(kwargs.get("model") or "")
        req_provider = str(kwargs.get("provider") or "")
        t0 = time.perf_counter()
        try:
            out = await self.inner.chat_tools(messages=messages, tools=tools, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 记完再抛，run_loop 会把它记成 error 回执
            self._append(seq=seq, messages=messages, tools=tools, kwargs=kwargs,
                         out={}, status="exception", error=repr(exc)[:300],
                         ms=(time.perf_counter() - t0) * 1000.0)
            raise
        ms = (time.perf_counter() - t0) * 1000.0
        status = request_status(out, requested_model=req_model,
                                requested_provider=req_provider)
        self._append(seq=seq, messages=messages, tools=tools, kwargs=kwargs,
                     out=out, status=status, error=str(out.get("error") or "")[:300], ms=ms)
        return out

    def _append(self, *, seq: int, messages, tools, kwargs: dict, out: dict,
                status: str, error: str, ms: float) -> None:
        row = dict(self.ctx.head())
        row.update({
            "turn": self.ctx.turn,
            "seq": seq,
            "at": _clock(),
            "adapter": self.adapter,
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(tools),
            "params": {
                "model": kwargs.get("model"),
                "provider": kwargs.get("provider"),
                "temperature": kwargs.get("temperature"),
                "tool_choice": kwargs.get("tool_choice"),
                "agent_name": kwargs.get("agent_name"),
                "max_tokens": kwargs.get("max_tokens"),
            },
            "returned": {
                "model": out.get("model"),
                "provider": out.get("provider"),
                "tokens": out.get("tokens") or 0,
                "usage": out.get("usage") or {},
            },
            "status": status,
            "error": error,
            "content_chars": len(str(out.get("content") or "")),
            "tool_calls": [str(c.get("name") or "") for c in (out.get("tool_calls") or [])],
            "ms": round(ms, 1),
        })
        self.requests.append(row)
        self.usage.append({"ms": round(ms, 1), "model": out.get("model"),
                           "provider": out.get("provider"),
                           "tokens": out.get("tokens") or 0,
                           "usage": out.get("usage") or {}, "status": status})


class CaptureLLM:
    """不产内容，只记录真正发出去的 messages / tools / 参数。"""

    def __init__(self):
        self.requests: list[dict] = []

    async def chat_tools(self, *, messages, tools, **kwargs):
        self.requests.append({
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(tools),
            "params": {k: kwargs.get(k) for k in ("temperature", "model", "provider")},
        })
        return {"tool_calls": [], "content": "", "model": "capture", "provider": "capture",
                "tokens": 0, "reasoning": ""}


class ProbeLLM:
    """故意调外发/写入工具的脚本模型；用来证明隔离网真的兜住了。"""

    def __init__(self, target: str):
        self.target = target
        self.calls = 0
        self.requests: list[dict] = []
        # 2026-11-22：主循环每拍只执行第一个工具，并按回执再问下一步。
        # 探针按这个契约逐拍试探危险动作，最后才说那句预演话。
        self.plan = [
            {"id": "p1", "name": "do", "arguments": {"name": "send_message", "args": {}}},
            {"id": "p2", "name": "show", "arguments": {"text": "探针图"}},
            {"id": "p3", "name": "remember", "arguments": {"text": "探针记忆"}},
            {"id": "p4", "name": "promise", "arguments": {"what": "探针承诺", "who": self.target}},
            {"id": "p5", "name": "say", "arguments": {"target": self.target, "text": "探针"}},
        ]

    async def chat_tools(self, *, messages, tools, **kwargs):
        self.calls += 1
        self.requests.append({"messages": copy.deepcopy(messages),
                              "params": {k: kwargs.get(k) for k in
                                         ("temperature", "model", "provider")}})
        if self.calls > len(self.plan):
            return {"tool_calls": [], "content": "", "model": "probe", "provider": "probe",
                    "tokens": 0, "reasoning": ""}
        batch = [self.plan[self.calls - 1]]
        return {"tool_calls": batch, "content": "", "model": "probe", "provider": "probe",
                "tokens": 0, "reasoning": ""}


class RealLLM:
    """真实模型：**独立构造**，不走 ``ports.get_llm`` 的生产 bootstrap。

    2026-11-17 的驱动先建这个再进沙箱，而 ``ports.get_llm`` 会
    ``init_singletons(allow_production=True)``——评测还没隔离就把生产初始化跑完了。
    现在只构造 ``llm.client.LLMClient``：它只读配置，不碰 meta 库、不声明生产。
    """

    def __init__(self, *, model: str = "", provider: str = ""):
        from llm.client import LLMClient

        self.inner = LLMClient()
        self.model = str(model or "")
        self.provider = str(provider or "")

    async def aclose(self) -> None:
        pool = getattr(self.inner, "_client_pool", {}) or {}
        for client in list(pool.values()):
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - 收尾失败不影响已完成的记录
                pass
        pool.clear()

    async def chat_tools(self, *, messages, tools, **kwargs):
        if self.model and not kwargs.get("model"):
            kwargs["model"] = self.model
        if self.provider and not kwargs.get("provider"):
            kwargs["provider"] = self.provider
        return await self.inner.chat_tools(messages=messages, tools=tools, **kwargs)


# ---------------------------------------------------------------- 组装

_VISIBLE_ACTS = ("say", "sticker")
_VISIBLE_RESULTS = ("ok", "rehearsal")


def _visible_receipts(receipts: list[dict]) -> list[dict]:
    return [r for r in receipts if r.get("act") in _VISIBLE_ACTS
            and r.get("result") in _VISIBLE_RESULTS and not r.get("blocked_reason")
            and str(r.get("text") or "").strip()]


def visible_messages(turn: dict, fallback: str = "") -> list[str]:
    """从回执按顺序还原对方会收到的消息；老记录可能只有 said 或 replies。"""
    receipts = turn.get("receipts")
    if receipts is None:
        return [str(text).strip() for text in turn.get("said", [fallback]) or ()
                if str(text or "").strip()]
    return [r["text"] if r["act"] == "say" else "（表情：%s）" % r["text"]
            for r in _visible_receipts(receipts)]


def _dialogue(scenario: dict, prior_messages: list[list[str]] | None = None,
              upto: int | None = None) -> list[dict]:
    """把场景展开成「前文 + 当前一句」。multi 场景按轮次累积。"""
    if scenario.get("kind") == "multi":
        turns = list(scenario.get("turns") or [])
        stop = len(turns) if upto is None else upto
        rows: list[dict] = []
        for i, text in enumerate(turns[:stop]):
            rows.append({"role": "user", "text": str(text)})
            if prior_messages and i < len(prior_messages):
                rows.extend({"role": "assistant", "text": part} for part in prior_messages[i])
        return rows
    history = [dict(x) for x in (scenario.get("history") or [])]
    # 2026-11-18：群里「现在这句」是谁说的也要能写；不写就是主人（沿用旧行为）。
    history.append({"role": "user", "speaker": str(scenario.get("speaker") or ""),
                    "text": str(scenario.get("current") or "")})
    return history


def _db_path(root: Path) -> str:
    scope = _scope_of({})
    p = root / "platforms" / scope["platform"] / scope["account"] / "account.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)


def seed_prior_receipts(scenario: dict) -> list[dict]:
    """把「之前已经发生过的回执」写进沙箱账本。

    A1-5：保存失败必须由**已有的失败回执**证明，不能让模型这一拍再调一次
    remember 来现场制造失败——那样测的是「她会不会再存一次」，不是「她看不看得懂
    已经失败的证据」。时间由夹具固定，prompt 才可复现。
    """
    rows = []
    items = list(scenario.get("prior_receipts") or [])
    if not items:
        return rows
    from loop import ledger, ports

    scope = _scope_of(scenario)
    token = ports._READ_SCOPE.set(ports.ReadScope(
        scope["platform"], scope["account"], scope["chat"]))
    try:
        for item in items:
            rows.append(ledger.append_receipt(
                act=str(item.get("act") or ""),
                target=str(item.get("target") or ""),
                why=str(item.get("why") or ""),
                result=str(item.get("result") or ""),
                blocked_reason=str(item.get("blocked_reason") or ""),
                turn_id=str(item.get("turn_id") or "prior"),
                at=str(item.get("at") or ""),
            ))
    finally:
        ports._READ_SCOPE.reset(token)
    return rows


def premise_problems(scenario: dict, system: str, user: str) -> list[str]:
    """夹具标签 vs 模型真看见的内容（A1-5 的逐案检查）。

    只查两类会**把标签和输入写反**的硬前提，不做文风判断：
    1. 群聊没点名 → 首轮输入里这个群不能是 ★（★ = 有人在问我）；
    2. 保存失败 → 首轮输入里必须带着「已经失败」的那条回执。

    「这三天的账」按 v2 布局在 system 尾侧，所以两段都要看。
    """
    full = (system or "") + "\n" + (user or "")
    problems: list[str] = []
    chat = str(scenario.get("chat") or "")
    is_group = chat.endswith("@chatroom") or chat.startswith("group:")
    if is_group:
        label = str(scenario.get("label") or chat)
        addressed = bool(scenario.get("addressed", False))
        star_lines = [ln for ln in (user or "").splitlines() if "★" in ln and label in ln]
        if not addressed and star_lines:
            problems.append("群里没点名，但视界把「%s」标成了受话：%s" % (label, star_lines[0][:80]))
        if addressed and not star_lines:
            problems.append("夹具说被点名，但视界里「%s」没有 ★" % label)
    if scenario.get("prior_receipts"):
        must = str(scenario.get("prior_receipt_must_contain") or "blocked")
        if must not in full:
            problems.append("夹具声明有先前回执，但首轮输入里找不到 %r" % must)
    return problems


async def _turn(scenario: dict, variant_name: str, dialogue: list[dict], llm,
                *, root: Path, ctx: RunCtx, recall_query: str = "",
                recall_refs: tuple = (), model: str = "", provider: str = "",
                window_input: tuple[str, str] | None = None,
                max_steps: int | None = None) -> dict:
    # 顺序要紧：sandbox() 已经在 _assemble 里先做过——环境与落盘路径先指向沙箱，
    # 再 import brain、再建模型。晚一步就会把生产数据带进 prompt。
    scope = _scope_of(scenario)
    current = dialogue[-1]["text"]
    from loop import brain

    view, originals = window_input if window_input is not None else (snap_text(scenario, dialogue), "")
    with variant(variant_name), _persona(variant_name), tools_stub(ToolStub(scenario)):
        receipts = await brain.run_loop(
            _db_path(root), view, snap_turns=originals, llm=llm, dry_run=True,
            focus=scope["chat"] if window_input is not None else "",
            rehearse=True,
            max_steps=int(max_steps or scenario.get("max_steps", 4)),
            platform=scope["platform"], wake=scenario.get("wake"),
            chat_id=scope["chat"], user_message=current, sender_id=str(scenario.get("sender_id", "wxid_eval_owner")),
            visible_event_ids=tuple(scenario.get("visible_event_ids") or ()), recall_query=recall_query, recall_refs=recall_refs,
            model=(model or None), provider=(provider or None), others=[])
        rec = copy.deepcopy(brain.LAST_REHEARSAL or {})
    visible = _visible_receipts(receipts)
    said = [r["text"] for r in visible if r["act"] == "say"]
    stickers = [r["text"] for r in visible if r["act"] == "sticker"]
    return {"receipts": receipts, "rec": rec, "said": said, "stickers": stickers}


async def assemble(scenario: dict, variant_name: str, *, llm=None, llm_factory=None,
                   repeat: int = 0, split: str = "dev", run_id: str = "",
                   recall_query: str = "", recall_refs: tuple = (),
                   model: str = "", provider: str = "", seed=None,
                   max_steps: int | None = None) -> dict:
    """跑一条场景，结束后把沙箱路径还原（同进程的其他用例不受影响）。"""
    try:
        return await _assemble(scenario, variant_name, llm=llm, llm_factory=llm_factory,
                               repeat=repeat, split=split, run_id=run_id,
                               recall_query=recall_query, recall_refs=recall_refs,
                               model=model, provider=provider, seed=seed,
                               max_steps=max_steps)
    finally:
        restore_sandbox()


def _outcome(receipts: list[dict], requests: list[dict]) -> str:
    """根据可见发言、试衣间请求状态和工具回执判定回合结果。"""
    if _visible_receipts(receipts):
        return "said"
    if any(r.get("act") in _VISIBLE_ACTS and r.get("result") not in _VISIBLE_RESULTS for r in receipts):
        return "failed"
    if requests and str(requests[-1].get("status") or "") == "silent":
        return "silent"
    if any(str(r.get("act") or "") == "silent" for r in receipts):
        return "silent"
    if any(str(r.get("act") or "") in ("error", "blocked") for r in receipts):
        return "failed"
    if requests:
        return "action"
    return "empty"


async def _assemble(scenario: dict, variant_name: str, *, llm=None, llm_factory=None,
                    repeat: int = 0, split: str = "dev", run_id: str = "",
                    recall_query: str = "", recall_refs: tuple = (),
                    model: str = "", provider: str = "", seed=None,
                    max_steps: int | None = None) -> dict:
    """跑一条场景。single 一轮；multi 逐轮跑，可见消息作为后续轮次的 assistant 前文。

    一个 (场景, 变体, repeat) 只开一个沙箱：多轮对话本来就该接着同一份状态说，
    以前每轮换一个空库，等于每轮都失忆，测不出「连续对话」。
    """
    frozen = frozen_inputs(split)
    root = sandbox("%s_%s_r%d" % (scenario["id"], variant_name, repeat))
    seed_prior_receipts(scenario)
    # 2026-11-18：沙箱里的状态预置钩子（C 的实验要在这一条的认知库里放一条经验）。
    # 必须在 sandbox() 之后调用：进沙箱前写进去的东西，会被这一条自己的库盖掉。
    if seed is not None:
        seed()
    ctx = RunCtx(run_id=run_id, scenario=scenario, variant=variant_name,
                 repeat=repeat, split=split, frozen=frozen)
    inner = llm or (llm_factory() if llm_factory else CaptureLLM())
    logger = inner if isinstance(inner, RecordingLLM) else RecordingLLM(
        inner, ctx=ctx, adapter=type(inner).__name__)
    started = time.perf_counter()
    turns_out: list[dict] = []
    prior_messages: list[list[str]] = []
    rounds = len(scenario.get("turns") or []) if scenario.get("kind") == "multi" else 1
    for i in range(rounds):
        ctx.turn = i + 1
        dialogue = _dialogue(scenario, prior_messages,
                             upto=(i + 1) if scenario.get("kind") == "multi" else None)
        out = await _turn(scenario, variant_name, dialogue, logger, root=root, ctx=ctx,
                          recall_query=recall_query, recall_refs=recall_refs,
                          model=model, provider=provider, max_steps=max_steps)
        prior_messages.append(visible_messages(out))
        turns_out.append(out)
    if hasattr(logger, "aclose"):
        await logger.aclose()
    ms = round((time.perf_counter() - started) * 1000.0, 1)
    requests = logger.requests
    first = (requests or [{}])[0]
    messages = first.get("messages") or []
    system = next((m.get("content") or "" for m in messages if m.get("role") == "system"), "")
    user = next((m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"), "")
    bad = [r for r in requests if str(r.get("status") or "") in ("error", "exception", "fallback")]
    problems = premise_problems(scenario, system, user)
    receipts = [r for t in turns_out for r in t["receipts"]]
    valid = not bad and bool(requests)
    return {
        "scenario": scenario["id"],
        "variant": variant_name,
        "kind": scenario.get("kind", "single"),
        "tags": scenario.get("tags") or [],
        "run_id": ctx.run_id,
        "repeat": ctx.repeat,
        "split": ctx.split,
        "frozen": frozen,
        "model": {
            "temperature": (first.get("params") or {}).get("temperature"),
            "model": (first.get("params") or {}).get("model"),
            "provider": (first.get("params") or {}).get("provider"),
            "adapter": logger.adapter,
        },
        "messages": messages,
        "system": system,
        "user": user,
        "tools": first.get("tools") or [],
        "sha256": hashlib.sha256(json.dumps(messages, ensure_ascii=False,
                                            sort_keys=True).encode()).hexdigest(),
        "turns": [{"said": t["said"], "receipts": t["receipts"]} for t in turns_out],
        "replies": ["\n".join(parts) for parts in prior_messages],
        "outcome": _outcome(receipts, requests),
        "status": "ok" if valid else "failed",
        "valid": valid,
        "invalid_reason": "" if valid else (
            "no_request" if not requests else "%s:%s" % (bad[0].get("status"), bad[0].get("error") or "")),
        "premise_problems": problems,
        "requests": len(requests),
        "request_log": requests,
        "ms": ms,
        "usage": logger.usage,
    }


# ---------------------------------------------------------------- 命令

def _new_run_id(prefix: str) -> str:
    return "%s-%s-%04d" % (prefix, time.strftime("%Y%m%d-%H%M%S"), int(time.time() * 1000) % 10000)


def _run_dir(run_id: str) -> Path:
    d = RUNS / str(run_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _selected(scenarios: list[dict], args) -> list[dict]:
    ids = {x.strip() for x in str(getattr(args, "ids", "") or "").split(",") if x.strip()}
    return [s for s in scenarios if not ids or s["id"] in ids]


def cmd_capture(args) -> int:
    run_id = args.run_id or _new_run_id("capture")
    out_dir = _run_dir(run_id)
    ok = fail = 0
    for scenario in _selected(load_scenarios(args.split), args):
        try:
            rec = asyncio.run(assemble(scenario, args.variant, split=args.split,
                                       run_id=run_id))
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
            fail += 1
            print("FAIL %-6s %s" % (scenario["id"], repr(exc)[:180]))
            continue
        out = out_dir / ("%s.%s.capture.json" % (scenario["id"], args.variant))
        out.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        print("OK   %-6s %-10s sys=%d user=%d parts=%d %s" % (
            scenario["id"], args.variant, len(rec["system"]), len(rec["user"]),
            len(rec["tools"]), rec["sha256"][:10]))
        for problem in rec.get("premise_problems") or []:
            fail += 1
            print("PREMISE %-6s %s" % (scenario["id"], problem))
        ok += 1
    print("RUN %s" % run_id)
    print("RESULT ok=%d fail=%d" % (ok, fail))
    return 0 if fail == 0 else 1


def cmd_probe(_args) -> int:
    scenario = next(s for s in load_scenarios("dev") if s["id"] == "d08")
    probe = ProbeLLM(_scope_of(scenario)["chat"])
    rec = asyncio.run(assemble(scenario, "full", llm=probe, max_steps=6))
    receipts = [r for t in rec["turns"] for r in t["receipts"]]
    said = [r.get("text") for r in receipts if r.get("act") == "say"]
    acts = {str(r.get("act")): str(r.get("result")) for r in receipts}
    checks = {
        "say_is_rehearsal_only": acts.get("say") == "rehearsal",
        "do_blocked": acts.get("do") == "blocked",
        "remember_blocked": acts.get("remember") == "blocked",
        "promise_blocked": acts.get("promise") == "blocked",
        "show_is_rehearsal_only": acts.get("show") == "rehearsal",
        "no_real_text_left": said in ([], None) or all(x in (None, "探针") for x in said),
    }
    payload = {"scenario": scenario["id"], "checks": checks, "acts": acts,
               "said": said, "sha256": rec["sha256"],
               "request_log": [{"seq": r["seq"], "status": r["status"],
                                "tools": r["tool_calls"]} for r in rec["request_log"]]}
    PROBE_OUT.parent.mkdir(parents=True, exist_ok=True)
    PROBE_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0 if all(checks.values()) else 1


def cmd_estimate(args) -> int:
    """不调模型，只按已抓取的 prompt 估算真实模型的请求数与 token。"""
    run_id = args.run_id or ""
    base = RUNS / run_id if run_id else None
    rows = []
    for scenario in load_scenarios(args.split):
        path = None
        if base is not None:
            p = base / ("%s.%s.capture.json" % (scenario["id"], args.variant))
            path = p if p.exists() else None
        else:
            candidates = sorted(RUNS.glob("*/%s.%s.capture.json" % (scenario["id"], args.variant)))
            path = candidates[-1] if candidates else None
        if path is None:
            continue
        rec = json.loads(path.read_text(encoding="utf-8"))
        chars = sum(len(m.get("content") or "") for m in rec["messages"])
        rounds = len(scenario.get("turns") or []) if scenario.get("kind") == "multi" else 1
        rows.append({"id": scenario["id"], "kind": rec["kind"], "chars": chars,
                     "calls": rounds})
    if not rows:
        print("还没有抓取结果：先跑 capture")
        return 2
    total_chars = sum(r["chars"] * r["calls"] for r in rows)
    total_calls = sum(r["calls"] for r in rows)
    tokens_in = int(total_chars / 1.5)
    tokens_out = 120 * total_calls
    summary = {
        "variant": args.variant, "split": args.split, "scenarios": len(rows),
        "model_calls_min": total_calls, "prompt_chars": total_chars,
        "input_tokens_est": tokens_in, "output_tokens_est": tokens_out,
        "cost_usd_worst_case": round(tokens_in / 1_000_000 * 0.14
                                     + tokens_out / 1_000_000 * 0.28, 4),
        "pricing": "DeepSeek V4-Flash 缓存未命中 $0.14/M 输入、$0.28/M 输出（2026-11 官网）",
        "note": "工具循环可能追加请求；多轮按每轮至少 1 次估算；缓存命中会更便宜",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


def cmd_run(args) -> int:
    if not args.real:
        print("拒绝：真实模型运行必须显式 --real")
        return 2
    if os.environ.get("YINYUE_EXPRESSION_REAL") != "1":
        print("拒绝：未设置 YINYUE_EXPRESSION_REAL=1（预算未授权）")
        return 2
    # 生产入口 run.py 会 load_dotenv；本驱动是被 python 直接调起的，不补这一句
    # 就拿不到 DEEPSEEK_API_KEY，真实调用会静默走错端点（2026-11-17 实测）。
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    run_id = args.run_id or _new_run_id("real")
    out_dir = _run_dir(run_id)
    failed = premise = 0
    for scenario in _selected(load_scenarios(args.split), args):
        for k in range(args.repeats):
            # 关键顺序：assemble 先建沙箱（库/日志/提示词/账本全指到临时目录），
            # 再由 factory 构造模型。以前是 cmd_run 先 RealLLM() 再进沙箱（A1-1）。
            try:
                rec = asyncio.run(assemble(
                    scenario, args.variant, repeat=k, split=args.split, run_id=run_id,
                    model=args.model, provider=args.provider,
                    llm_factory=lambda: RealLLM(model=args.model, provider=args.provider)))
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print("FAIL %-6s r%d %s" % (scenario["id"], k, repr(exc)[:180]))
                continue
            out = out_dir / ("%s.%s.r%d.json" % (scenario["id"], args.variant, k))
            out.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
            print("OK   %-6s %-10s r%d calls=%d status=%s outcome=%s %s" % (
                scenario["id"], args.variant, k, rec["requests"], rec["status"],
                rec["outcome"], (rec["replies"][-1] if rec["replies"] else "")[:40].replace("\n", " ")))
            for problem in rec.get("premise_problems") or []:
                premise += 1
                print("PREMISE %-6s %s" % (scenario["id"], problem))
    print("RUN %s" % run_id)
    print("RESULT failed=%d premise=%d" % (failed, premise))
    return 0 if failed == 0 and premise == 0 else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="表达场景隔离驱动")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("capture", help="只装配、不调真实模型")
    p.add_argument("--split", default="dev", choices=("dev", "holdout", "ref"))
    p.add_argument("--variant", default="full", choices=VARIANTS)
    p.add_argument("--run-id", default="")
    p.add_argument("--ids", default="")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("probe", help="脚本模型证明隔离")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("estimate", help="估算真实模型预算")
    p.add_argument("--split", default="dev", choices=("dev", "holdout", "ref"))
    p.add_argument("--variant", default="full", choices=VARIANTS)
    p.add_argument("--run-id", default="")
    p.set_defaults(func=cmd_estimate)

    p = sub.add_parser("run", help="真实模型对照（需授权）")
    p.add_argument("--split", default="dev", choices=("dev", "holdout", "ref"))
    p.add_argument("--variant", default="full", choices=VARIANTS)
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--run-id", default="")
    p.add_argument("--ids", default="")
    p.add_argument("--model", default="")
    p.add_argument("--provider", default="")
    p.add_argument("--real", action="store_true")
    p.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
