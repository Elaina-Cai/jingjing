# -*- coding: utf-8 -*-
"""about 评测的离线重放：用评测集文本直接问 N1 读场问句，对比标签（不改生产链路）。"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ABOUT = {"her", "speaker", "other", "room", "unknown"}
HER_NAMES = {"鲸鲸", "yinyue"}

SYSTEM = (
    "你是群聊理解层（N1）。只输出一个 JSON 对象，不要 Markdown、不要额外说明：\n"
    "{\"scene_read\": {\"about\": {\"subject\": \"speaker|other|her|room|unknown\", \"her\": true|false|null, \"confidence\": 0..1, \"evidence\": [\"依据\"]}, \"thread_continue\": true|false|null}}\n"
    "判断 subject 的依据是这条消息的内容主体是谁（不是在跟谁说话）：\n"
    "- speaker：内容主体是说话人自己（我的状态、我的安排、我的感受、我的东西）；\n"
    "- other：内容主体是另一个具体的人（对他说、答他、夸他、损他、问他、提到他）；明显在跟群里某个人对话（没写名字）也算；\n"
    "- room：面向大家的普遍情况或提问（你们…都…、今天大家…），或者没有任何具体对象（纯图、单字、口头禅、无内容）；\n"
    "- her：内容主体是鲸鲸（在对她说话、答复她、叫她、提到她、AI/机器人指她）；窗口里她刚说过话/被拍/被引用且当前消息接着这个话头也算；\n"
    "- unknown：文字读不懂（不是没对象，是无法理解）；嗯、哦 这类应和算 room。\n"
    "her：这轮是否需要她/围绕她（被召唤、在对她说、接着她的话头给 true；明显与她无关给 false；判不出给 null）。\n"
    "confidence：subject 的把握度 0..1。evidence：判定依据，最多 3 条。\n"
    "补充规则：\n"
    "1) @ 谁不决定 subject：@ 某人 + 讲自己的事 -> speaker；@ 某人 + 问/说对方的事 -> other。\n"
    "2) 先说整体或大家、再带一句自己 -> room；通篇只讲自己 -> speaker。\n"
    "3) 直接对鲸鲸说 你、叫她的名字、答复她 -> subject=her。\n"
    "4) 求取关、我不认领、不是我说的 这类自述 -> speaker；纯标点/纯表情 -> room。\n"
    "示例：\n"
    "@鲸鲸 你能帮上忙吗 -> subject=her, her=true\n"
    "你刚才说什么来着 -> subject=her, her=true\n"
    "成，我先自己掂，真缺了再喊你 -> subject=speaker, her=false\n"
    "求取关 -> subject=speaker, her=false\n"
    "网站是啥来着 -> subject=other, her=false\n"
    "那你会过日子 -> subject=other, her=false\n"
    "你们下午都干嘛？我就刨了十趟 -> subject=room, her=false\n"
    "看来下午就是全民干瞪眼的时间，我也只剩 4 点 -> subject=room, her=false\n"
    "嗯嗯 -> subject=room, her=false\n"
    "thread 示例：你刚才说什么来着 -> thread_continue=true；帮我写物理作业 -> thread_continue=false\n"
)


def _prompt(case: dict) -> str:
    ctx = case.get("ctx") or []
    lines = ["【窗口】"] + ([f"- {c}" for c in ctx] if ctx else ["（无）"])
    lines += ["", "【当前消息】", str(case.get("text") or "")]
    return "\n".join(lines)


def _parse(raw: str):
    text = str(raw or "")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None
    section = obj.get("scene_read") if isinstance(obj.get("scene_read"), dict) else obj
    about = section.get("about")
    if isinstance(about, dict):
        subject = str(about.get("subject") or "unknown").strip().lower()
        her = about.get("her")
        if not isinstance(her, bool):
            her = None
        conf = about.get("confidence")
        conf = float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else 0.0
        about_out = {"subject": subject, "her": her, "confidence": conf,
                     "evidence": list(about.get("evidence") or [])}
    else:
        raw_about = str(about or "").strip()
        mapped = {"她": "her", "鲸鲸": "her"}.get(raw_about, raw_about)
        about_out = mapped.lower()
    tc = section.get("thread_continue")
    return {"about": about_out, "thread_continue": tc}


async def _run(args) -> int:
    from dotenv import load_dotenv
    load_dotenv(pathlib.Path(".env"))
    from llm.client import LLMClient
    llm = LLMClient()
    fixture = json.loads(pathlib.Path(args.fixture).read_text(encoding="utf-8"))
    cases = fixture["cases"]
    if args.ids:
        want = {x.strip() for x in args.ids.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in want]
    if args.limit:
        cases = cases[: args.limit]
    rows = []
    for case in cases:
        try:
            raw = await llm.chat(system=SYSTEM, user=_prompt(case), agent_name="about_eval",
                                 temperature=0.1, max_tokens=200)
        except Exception as exc:
            raw = f"ERROR {type(exc).__name__}: {exc}"
        pred = _parse(str(raw))
        rows.append({"id": case["id"], "text": case["text"], "label": case["labels"],
                     "pred": pred, "raw": str(raw)[:300]})
        print(f"{case['id']:<5} label={case['labels']['about']:<8} pred={(pred or {}).get('about')} "
              f"| tc label={case['labels'].get('thread_continue')} pred={(pred or {}).get('thread_continue')}")
    out = pathlib.Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"fixture": args.fixture, "rows": rows}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    about_n = about_ok = her_n = her_ok = tc_n = tc_ok = 0
    miss = {}
    her_miss = {}
    for row in rows:
        pred = row["pred"] or {}
        lab = row["label"]
        p = str(pred.get("about") or "").lower()
        if isinstance(pred.get("about"), dict):
            p = str(pred["about"].get("subject") or "").lower()
        l = lab.get("about")
        if l in ABOUT and p in ABOUT:
            about_n += 1
            if p == l:
                about_ok += 1
            else:
                miss[(l, p)] = miss.get((l, p), 0) + 1
        ph = None
        if isinstance(pred.get("about"), dict):
            ph = pred["about"].get("her")
        if ph is not None and not isinstance(ph, bool):
            ph = None
        lh = lab.get("her")
        if isinstance(lh, bool) or isinstance(ph, bool):
            her_n += 1
            if bool(lh) == bool(ph):
                her_ok += 1
            else:
                her_miss[(str(lh), str(ph))] = her_miss.get((str(lh), str(ph)), 0) + 1
        lt = lab.get("thread_continue")
        pt = pred.get("thread_continue")
        if isinstance(lt, bool) or isinstance(pt, bool):
            tc_n += 1
            if bool(lt) == bool(pt):
                tc_ok += 1
    print()
    print(f"cases={len(rows)}")
    print(f"subject n={about_n} agree={about_ok} rate={about_ok / about_n if about_n else 0:.3f}")
    print(f"her     n={her_n} agree={her_ok} rate={her_ok / her_n if her_n else 0:.3f}")
    print(f"thread  n={tc_n} agree={tc_ok} rate={tc_ok / tc_n if tc_n else 0:.3f}")
    for (l, p), n in sorted(miss.items(), key=lambda kv: -kv[1]):
        print(f"  miss {l} -> {p}: {n}")
    if her_miss:
        print("her misses:", her_miss)
    print()
    print("=== mismatch rows ===")
    for row in rows:
        pred = row["pred"] or {}
        lab = row["label"].get("about")
        p = str(pred.get("about") or "").lower()
        if isinstance(pred.get("about"), dict):
            p = str(pred["about"].get("subject") or "").lower()
        if lab in ABOUT and p in ABOUT and lab != p:
            print(f"  {row['id']:<5} label={lab:<8} pred={p:<8} | {str(row['text'])[:60]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", default="tests/fixtures/about_eval.json")
    ap.add_argument("--ids", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", default="output/about_replay.json")
    args = ap.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
