# -*- coding: utf-8 -*-
# 语体 A/B：只动「人设卡」，其余（四道墙、怎么开口、场景）逐字不变，看 5 个显著指标动没动。
#
#   python scripts\voice_ab.py smoke            # 3 个臂各 1 条，看接线和样子
#   python scripts\voice_ab.py run [每场景条数]   # 默认 5 → 3 臂 × 20 场景 × 5 = 300 条
#
# 对照基准是 .test_tmp\voice_bootstrap.json 里真人等样本自举出来的 均值±sd（不是拍脑袋的目标值）。
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from llm.client import LLMClient  # noqa: E402
from loop.brain import WALLS  # noqa: E402
from voice_eval import metrics  # noqa: E402

CARD = os.path.join(ROOT, "data", "persona", "cards", "鲸鲸.md")
OUT = os.path.join(ROOT, ".test_tmp", "voice_ab")
from core.policy.channel import render as _channel_render  # noqa: E402

KAIKOU = _channel_render()

ADD_V2 = """## 零、怎么接话（优先于下面所有关于语气的话）

- 一条只说一件事。说完了就发，不要把两件事塞进同一条。
- 一句话能说完就说完，不要切两半；讲一件事的来龙去脉时可以写到三十字。
- 可以反复用同几个开头、同几个词——「行」「哦？」「我不」「有点」这类，你本来就有口头禅，别每句都换新说法。
- 想问就问，不必等到有把握；真人聊天里大约 14% 的话带问号。
- 别拿省略号吊着收尾，「…」三十条里最多一条。

"""
SEEDS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          ".test_tmp", "voice_ab", "seeds.json")


def load_seeds():
    """真实历史种子：一句对方原话 + 它前面那一屏（谁说了什么）。不是我编的场景。"""
    with open(SEEDS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def cards():
    src = open(CARD, encoding="utf-8").read()
    v1 = re.sub(r"^\*\*要点\*\*：.*\n", "", src, flags=re.M)
    v2 = v1.replace("## 一、我怎么说话", ADD_V2 + "## 一、我怎么说话", 1)
    os.makedirs(os.path.join(ROOT, ".test_tmp", "cards"), exist_ok=True)
    out = {}
    for name, text in (("V0", src), ("V1", v1), ("V2", v2), ("V3", src)):
        p = os.path.join(ROOT, ".test_tmp", "cards", name + ".md")
        open(p, "w", encoding="utf-8", newline="\n").write(text)
        out[name] = text
    return out


def system_for(card_text, kaikou=None):
    return card_text + "\n\n" + WALLS + "\n\n" + (kaikou or KAIKOU)


def user_for(seed):
    """把种子拼成她真实看到的那一屏：时间 + 会话类型 + 最近几条谁说的。"""
    lines = ["现在是%s，%s。" % (seed.get("clock") or "晚上九点", seed.get("where") or "私聊"),
             "最近这一屏（从上到下）："]
    for who, txt in seed.get("screen") or []:
        lines.append("%s：%s" % (who, txt))
    return "\n".join(lines)


async def one(llm, sys_p, usr, sem, temp=0.7):
    """一次发言。走生产同一条 chat_tools，只取 say 的 text；没 say = 她选择沉默（返回空串单列统计）。"""
    from loop.brain import SCHEMAS  # 2026-11-02 修 F821：工具清单只有一个权威定义处
    async with sem:
        for i in range(3):
            try:
                native = await llm.chat_tools(system=sys_p, user=usr, tools=SCHEMAS,
                                              tool_choice="auto", agent_name="loop",
                                              temperature=temp, max_tokens=300)
                said = []
                for c in (native.get("tool_calls") or []):
                    name = c.get("name") or (c.get("function") or {}).get("name") or ""
                    args = c.get("arguments")
                    if not isinstance(args, dict):
                        try:
                            args = json.loads(args or "{}")
                        except Exception:  # noqa: BLE001
                            args = {}
                    if name == "say":
                        t = str(args.get("text") or "").strip()
                        if t:
                            said.append(t)
                if not said:  # 有的回合她直接写正文、不调 say（生产里这算自语，实验里它同样是“说法”，收下）
                    _t = str(native.get("content") or "").strip()
                    if _t:
                        said.append(_t)
                return "\n".join(said)
            except Exception as exc:  # noqa: BLE001
                if i == 2:
                    return "!!ERR " + repr(exc)[:200]
                await asyncio.sleep(1.5 * (i + 1))


async def gather(arms, per_scene, temp=0.7, conc=6):
    llm = LLMClient()
    sem = asyncio.Semaphore(conc)
    jobs = []
    for arm, card_text in arms.items():
        sp = system_for(card_text, KAIKOU.replace("每条十几字", "想一句说完就说一句，二三十字都正常") if arm == "V3" else KAIKOU)
        for scene in load_seeds():
            for k in range(per_scene):
                # 实验控制：三个臂加同一句，避免“她选择这拍不说话”把样本吃掉（沉默率另用真语料统计）
                usr = user_for(scene) + "\n（这一拍就直接开口，别调别的工具，也别解释。）"
                jobs.append((arm, scene, k, one(llm, sp, usr, sem, temp)))
    t0 = time.time()
    texts = await asyncio.gather(*[j[3] for j in jobs])
    return [{"arm": j[0], "scene": j[1].get("scene", ""), "k": j[2], "text": t} for j, t in zip(jobs, texts)], round(time.time() - t0, 1)


def as_msgs(text):
    """一条 say 文本 = 几个气泡：按空行切，最多 3 个（与 loop/ports.split_bubbles 同规则）。"""
    parts = [x.strip() for x in (text or "").split("\n\n")]
    return [x for x in parts if x][:3]


def evaluate(rows):
    out = {}
    for arm in sorted({r["arm"] for r in rows}):
        got = [r for r in rows if r["arm"] == arm and not r["text"].startswith("!!ERR")]
        msgs = [m for r in got for m in as_msgs(r["text"])]
        out[arm] = dict(metrics(msgs), samples=len(got), errs=sum(1 for r in rows if r["arm"] == arm and r["text"].startswith("!!ERR")))
    return out


def score(arms_m):
    ref = json.load(open(os.path.join(ROOT, ".test_tmp", "voice_bootstrap.json"), encoding="utf-8"))
    rows = ref["rows_her_mean_sd_z"]
    keys = ["ellipsis_ratio", "avg_sent_len", "sent_per_msg", "top_open_share", "q_ratio"]
    res = {}
    for arm, m in arms_m.items():
        detail, hit = {}, 0
        for k in keys:
            her, mu, sd = rows[k][0], rows[k][1], rows[k][2]
            gap, moved = mu - her, m[k] - her
            pct = moved / gap if gap else 0.0
            z = (m[k] - mu) / sd if sd else 0.0
            detail[k] = {"her": her, "human": mu, "arm": m[k], "moved_pct": round(pct, 2), "z_vs_human": round(z, 1)}
            if pct >= 0.5:
                hit += 1
        res[arm] = {"moved_ge_50pct": hit, "of": len(keys), "pass": hit >= 3 and m.get("meta_sent_ratio", 0) <= 0.009,
                    "metrics": detail}
    return res


def blind(rows, a="V0", b="V2", n=30):
    rng = random.Random(20261028)
    pa = [r for r in rows if r["arm"] == a and not r["text"].startswith("!!ERR")]
    pb = [r for r in rows if r["arm"] == b and not r["text"].startswith("!!ERR")]
    rng.shuffle(pa)
    rng.shuffle(pb)
    pairs, key, md = [], [], ["# 盲评：哪一条更像真人在说话？", "", "规则：只看 A/B 两段，选更像人的那条（不确定就写「平」）。不要猜哪个是新版。", ""]
    for i in range(min(n, len(pa), len(pb))):
        pick = (rng.random() < 0.5)
        left, right = (pa[i], pb[i]) if pick else (pb[i], pa[i])
        key.append({"i": i + 1, "A": left["arm"], "B": right["arm"], "scene": left["scene"]})
        md += ["## %d（场景：%s）" % (i + 1, left["scene"]), "", "A：", "", "```", left["text"].strip(), "```", "", "B：", "",
               "```", right["text"].strip(), "```", "", "选：____（A / B / 平）", ""]
        pairs.append([left["text"], right["text"]])
    open(os.path.join(OUT, "blind.md"), "w", encoding="utf-8", newline="\n").write("\n".join(md))
    open(os.path.join(OUT, "blind_key.json"), "w", encoding="utf-8", newline="\n").write(json.dumps(key, ensure_ascii=False, indent=1))
    return len(key)


def main(argv):
    mode = argv[0] if argv else "smoke"
    os.makedirs(OUT, exist_ok=True)
    arms = cards()
    per = 1 if mode == "smoke" else int(argv[1]) if len(argv) > 1 else 5
    rows, secs = asyncio.run(gather(arms, per))
    tag = "smoke" if mode == "smoke" else "run"
    with open(os.path.join(OUT, "raw_%s.jsonl" % tag), "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    m = evaluate(rows)
    out = {"mode": mode, "per_scene": per, "seconds": secs, "n": len(rows), "arms": m,
           "silent": {a: sum(1 for r in rows if r["arm"] == a and not str(r["text"]).strip())
                      for a in arms}}
    if mode != "smoke":
        out["score"] = score(m)
        out["blind_pairs"] = blind(rows)
    open(os.path.join(OUT, "summary_%s.json" % tag), "w", encoding="utf-8", newline="\n").write(
        json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False)[:1800])
    if mode == "smoke":
        for arm in ("V0", "V1", "V2"):
            s = [r["text"] for r in rows if r["arm"] == arm]
            print(json.dumps({"arm": arm, "text": s}, ensure_ascii=False)[:700])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
