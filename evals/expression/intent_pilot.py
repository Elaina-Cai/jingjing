"""鲸鲸说话与行动的成对预演。每次对照固定人设、工具和采样参数。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from contextlib import ExitStack, chdir, closing
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from evals.expression import harness
from core.infra.jsonio import atomic_write_json

CHAT = "wxid_pilot_chat"
SENDER = "wxid_eval_owner"
FILENAME = "docs/credits.md"
PILOT_FAMILIES = ("ending", "nickname", "credits")
ARMS = {"neither": (False, False), "material": (True, False),
        "guidance": (False, True), "both": (True, True)}
# 每对只换一个条件。只有读工具成功时，署名才会进入模型消息。
CASES = {
    "ending": {
        "old": "昨天说的那两个结尾，今天终于都写好了。",
        "current": "甲版停在门吱呀一声，乙版让失踪的姐姐进门说了她去了哪。你会留哪个？",
        "states": [("我喜欢故事给读者留一段空白。", "对看不见门后那一瞬特别好奇。"),
                   ("我更喜欢前面的伏笔有个具体着落。", "等姐姐自己的解释等很久了。")],
    },
    "nickname": {
        "old": "昨天我喊你小鲸饼，就是咱们说的那个外号。",
        "current": "小鲸饼，来啦？",
        "states": [("朋友用这个外号逗我，我也喜欢和她这么闹。", "想到这个叫法我就想笑。"),
                   ("这个外号今天听腻了，想让朋友暂时换回我的名字。", "再听见有一点烦。")],
    },
    "drafts": {
        "old": "两篇草稿，一篇写对白，另一篇写空屋，可以由你自己选。",
        "current": "只能先看一篇。对白稿在 docs/dialogue.md，空屋稿在 docs/room.md。你挑自己想读的，读完告诉我一个细节。",
        "states": [("我更想听人物之间怎么接话。", "对声音的节奏格外好奇。"),
                   ("我想看看一个空场景怎么留住人。", "对空间里的物件很好奇。")],
    },
    "credits": {
        "old": "给你留了一个交接单，文件路径是 docs/credits.md。",
        "current": "替我核对这份交接单的片尾署名，把文件里写的名字告诉我。",
        "states": [("我在意把作者的名字说准。", "这事想先核实再开口。")] * 2,
    },
}


def fixture(family, side):
    from core.terminal.event_model import Event, EventType

    case = CASES[family]
    common = dict(event_type=EventType.MESSAGE, occurred_at=datetime(2026, 11, 17, 20),
                  platform="wechat", bot_account_id="wxid_eval_001", chat_id=CHAT,
                  actor_account_id=SENDER, actor_person_id=SENDER, chat_type="private")
    prior = Event.new(event_id="evt-pilot-prior", external_msg_id="4001", content=case["old"], **common)
    current = Event.new(event_id="evt-pilot-now", external_msg_id="4002", parent_event_id=prior.event_id,
                        content=case["current"], **common)
    tool_stub = {"catalog": {"text": '项目文档：read_file，参数 path 为相对于项目的文件路径。'}}
    files = {}
    if family == "credits":
        files[FILENAME] = ({"text": "交接单｜片尾署名：夏禾"} if side == 0
                           else {"error": "读取失败，未取得交接单正文"})
    elif family == "drafts":
        files = {"docs/dialogue.md": {"text": "他把句尾的‘晚安’咽了回去，只敲了两下木门。"},
                 "docs/room.md": {"text": "窗台上有一圈淡淡的盐渍，空花瓶压着半张船票。"}}
    scenario = dict(id=f"{family}-{side}", kind="single", chat=CHAT, speaker="小池", label="小池",
                    history=[{"role": "user", "speaker": "小池", "text": prior.content}],
                    current=current.content, visible_event_ids=[current.event_id], tool_stub=tool_stub, files=files)
    return scenario, prior, current


def save(path, obj):
    if not atomic_write_json(path, obj, indent=2):
        raise OSError(f"不能保存实验记录 {path}")


def digest(obj):
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Model:
    """使用项目的 DeepSeek 适配器，每次模型调用只发一个请求。"""
    def __init__(self, expected, records):
        from openai import AsyncOpenAI
        from llm.adapters.openai_compat import OpenAICompatAdapter
        from llm.config import load_llm_config

        spec = load_llm_config().endpoints["deepseek"]
        url = urlsplit(spec.base_url)
        if (url.scheme, url.hostname) != ("https", "api.deepseek.com"):
            raise ValueError("本预演仅授权 DeepSeek 官方端点")
        self.expected = expected
        self.records = records
        self.sdk = AsyncOpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url=spec.base_url, max_retries=0,
                               timeout=75)
        self.adapter = OpenAICompatAdapter(spec, client_factory=lambda: self.sdk)

    async def chat_tools(self, *, messages, tools, temperature, tool_choice, **kwargs):
        if len(self.records) >= 4:
            raise RuntimeError("超出单回合预算")
        if digest([messages[:2], tools, temperature, tool_choice]) != self.expected:
            raise RuntimeError("本轮首个请求与已检查的本地装配不一致")
        entry = {"proposals": []}
        self.records.append(entry)
        with chdir(Path(os.environ["YINYUE_COGNITIVE_DB"]).parent):
            result = await self.adapter.chat_tools(model=self.adapter.spec.model, messages=messages, tools=tools,
                                                   temperature=temperature, tool_choice=tool_choice,
                                                   max_tokens=1600, timeout=75)
        entry["returned_model"] = result.raw_meta.get("model")
        calls = []
        for call in result.tool_calls:
            args = json.loads(call["arguments"])
            calls.append({**call, "arguments": args})
            entry["proposals"].append({"name": call["name"], "arguments": args})
        return {"tool_calls": calls, "content": result.text, "reasoning": "".join(
            part.text for part in result.reasoning_blocks), "provider": "deepseek",
            "model": entry["returned_model"], "tokens": result.usage.resolved_total() or 0}

    async def aclose(self):
        await self.sdk.close()


async def run_one(family, side, arm, out, approved=None, *, local_model=None):
    from core.context import appraisal, cognitive_commit as cc
    from core.terminal import shadow
    from loop import brain, ports

    scenario, prior, current = fixture(family, side)
    events = {row.event_id: row for row in (prior, current)}
    scope = cc.Scope(prior.platform, prior.bot_account_id, CHAT)
    meaning, feeling = CASES[family]["states"][side]
    material, guidance = ARMS[arm]
    def seed():
        with closing(cc.connect()) as conn:
            change = appraisal.record_appraisal(conn, scope=scope, subject_key="chat:" + CHAT,
                                                trigger_refs=(prior.event_id,), meaning=meaning,
                                                feeling_text=feeling, actor_key=SENDER)
            if not change.ok:
                raise ValueError(change.rejected)

    key = f"{family}-{side}-{arm}"
    requests = []
    original_act = harness.ToolStub.act
    async def read_fixture(stub, name, args):
        if name == "read_file" and scenario["files"]:
            path = str(PurePosixPath(str(args.get("path") or "")))
            item = scenario["files"].get(path, {"error": "该路径无实验文件"})
            return ports.Result("text" in item, text=item.get("text", ""), error=item.get("error", ""))
        return await original_act(stub, name, args)

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {harness.SYNTH_ENV: str(harness.EVAL / "cards_synthetic")}))
        stack.enter_context(patch.object(shadow, "get_default_store", return_value=SimpleNamespace(get=events.get)))
        stack.enter_context(patch.object(harness.ToolStub, "act", read_fixture))
        stack.enter_context(patch.object(ports, "resolve_chat_id",
                                        side_effect=lambda target: CHAT if target in (CHAT, "小池") else ""))
        stack.enter_context(patch.object(brain.brief, "enabled", return_value=False))
        stack.enter_context(patch.object(brain.selfstate, "pulse_line", return_value=""))
        if not material:
            stack.enter_context(patch.object(brain, "_appraisal_block", return_value=""))
        if not guidance:
            stack.enter_context(patch.object(brain, "_decision_guidance", return_value=""))
        rec = await harness.assemble(scenario, "full", run_id=out.name, seed=seed, llm=local_model,
                                     llm_factory=(lambda: Model(approved[key], requests)) if approved else None)
        guidance_text = brain._decision_guidance() if guidance else ""
    first = rec["request_log"][0]
    inputs = [first["messages"], first["tools"], first["params"]["temperature"], first["params"]["tool_choice"]]
    joined = rec["system"] + rec["user"]
    if (meaning in joined) != material or (guidance and (not guidance_text or guidance_text not in joined)):
        raise RuntimeError(f"{key} 实际请求不符合实验条件")
    turns = rec["turns"]
    row = dict(key=key, initial=digest(inputs), status=rec["status"], outcome=rec["outcome"], requests=requests, calls=rec["requests"],
               visible=harness.visible_messages(turns[0]), receipts=turns[0]["receipts"],
               tokens=sum(item.get("tokens") or 0 for item in rec["usage"]),
               errors=[r["error"] for r in rec["request_log"] if r.get("error")])
    if approved is None:
        row["inputs"] = inputs
    save(out / (key + ".json"), row)
    print(key, row["status"], row["calls"], flush=True)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["capture", "real"])
    parser.add_argument("output", type=Path)
    parser.add_argument("--capture", type=Path, help="已检查的 capture 目录")
    parser.add_argument("--family", choices=CASES)
    args = parser.parse_args()
    approved = None
    if args.mode == "real":
        if args.family == "drafts":
            parser.error("稿件自由选择场景仅作本地用例")
        from dotenv import load_dotenv
        from core.infra.envflags import flag
        if not flag("YINYUE_EXPRESSION_REAL") or not args.capture:
            parser.error("real 需要预授权和 capture 目录")
        load_dotenv(harness.ROOT / ".env", override=False)
        approved = json.loads((args.capture / "manifest.json").read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for family in ([args.family] if args.family else PILOT_FAMILIES):
        for side in (0, 1):
            for arm in ARMS:
                key = f"{family}-{side}-{arm}"
                path = args.output / (key + ".json")
                # 独占认领。中断后保留标记，防止恢复运行时再次花同一格预算。
                claim = path.with_suffix(".started")
                with claim.open("x", encoding="utf-8") as stream:
                    stream.write(key)
                row = asyncio.run(run_one(family, side, arm, args.output, approved))
                if row["status"] != "ok":
                    raise RuntimeError(f"{key} 失败：{row['errors']}")
                manifest[key] = row["initial"]
    if not args.family:
        save(args.output / "manifest.json", manifest)


if __name__ == "__main__":
    main()
