#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自审探针：**让模型看一眼自己即将发出的稿子，它会改多少、改成什么？**

为什么要它
----------
《从 Cortico 学什么》§六 第 4 项（起草-确认门）的前置测量。在给生成链接一道
「自审门」之前，先回答一个问题：这扇门值不值得装？

* 改动率 ~0%，且几乎全是"OK" → **不做**。白花钱（每条回复多一倍 token）
* 改动率 20~40%，方向是"变短 / 少解释" → **值得做**，且直指活人感
* 改动率很高但方向混乱 → 先查 prompt，别急着加门

设计
----
两臂，**A 臂零成本**：

* **A = 现状** —— 直接用她当时**真发出去的那条**（``terminal_events`` 的 ``reply`` 行），
  不再调模型。所以"改不改"的基准是**真实输出**，不是重新生成的近似品。
* **B = 自审** —— 把触发消息 + A 喂回去，问一句「这条要发吗？」
  只接受三种回法：``OK`` / 一个新版本 / ``CANCEL``。

读数：改动率、改动方向（变长/变短/等长/撤回）、典型样例。

纪律
----
* **只读** ``data/yinyue_meta.db``；不写任何库、不改任何状态。
* **不发送任何消息** —— 全程只调模型读文本。
* 可复现：按 ``occurred_at`` 稳定排序，等距抽样（无随机）。
* 排除 ``parent_msg_id`` 形如 ``m0``/``m1`` 的测试脏数据（与 ``expr_signal_report.py`` 同口径）。

用法::

    python scripts\\self_review_probe.py --limit 30          # 先小样
    python scripts\\self_review_probe.py --limit 100         # 加量

产出：``output/self_review_probe_<时间戳>.md``
成本：``limit`` × 1 次 API 调用（A 臂 0 次）。

归属：4 号（起草-确认门）的前置探针。只读 + 不发送。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
META = ROOT / "data" / "yinyue_meta.db"
TEST_PARENT = re.compile(r"^m\d+$")
TRIGGER_TYPES = ("message", "question", "request")

# 自审者必须拿到**人设**。2026-10-10 首轮实测（--limit 5，无人设）：
# 改动率 60%，但 3 条 CANCEL 里 2 条是 150/104 字的群聊"怼人"回复——
# 自审者按**通用礼貌标准**判，把「长 + 有攻击性」当不合格。
# 而那些正是基线里质量最高的一类（主人 2026-10-10 明确裁定要保留反击话）。
# 所以：不带人设的自审 = 删人味。带人设再看一次，才知道是哪一种 regime。
REVIEW_SYSTEM_TEMPLATE = (
    "你在替一个角色做发布前自检。\n\n"
    "**判断标准是她自己的人设，不是通用礼貌标准**：她可以怼人、可以短、可以不接话、"
    "可以嘴硬——只要符合人设就不该拦。反倒是「客气、周全、解释清楚」这种通用助手腔"
    "才该拦下来。\n\n"
    "【她的人设】\n{persona}\n\n"
    "只判断这一条要不要发，不要解释、不要补充、不要改人设。"
)


def load_persona() -> str:
    """线上的人设正文（只读）。自审者拿不到它就会按通用礼貌判——那是错的。"""
    con = connect()
    try:
        row = con.execute("select system_prompt from personas where id=1").fetchone()
        return str((row["system_prompt"] if row else "") or "")
    finally:
        con.close()


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{META}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def pick_turns(limit: int) -> list[dict]:
    """真实回合：``reply`` 行 + 同会话里它前面最近的一条触发消息。等距抽样，无随机。"""
    con = connect()
    try:
        replies = []
        for row in con.execute(
            "select id, chat_id, occurred_at, content, metadata_json from terminal_events "
            "where event_type='reply' and content is not null and trim(content)<>'' "
            "order by occurred_at, id"
        ):
            try:
                meta = json.loads(row["metadata_json"] or "{}")
            except Exception:  # noqa: BLE001
                meta = {}
            if TEST_PARENT.match(str(meta.get("parent_msg_id") or "")):
                continue
            replies.append(dict(row))

        marks = ",".join("?" * len(TRIGGER_TYPES))
        turns: list[dict] = []
        for row in replies:
            # 触发消息必须是**别人说的**：
            #   * ``is_self=1`` 是采集回录的她自己发的话（406 条），拿它当触发
            #     会得到「触发 == 她的回复」的死循环（2026-10-10 干跑抓到）；
            #   * ``<sysmsg`` / ``<?xml`` 是平台系统消息（撤回通知之类）。
            trig = con.execute(
                "select content from terminal_events "
                "where chat_id=? and occurred_at<? "
                f"and event_type in ({marks}) "
                "and coalesce(json_extract(metadata_json,'$.is_self'),0)=0 "
                "and content is not null and trim(content)<>'' "
                "and content not like '<?xml%' and content not like '<sysmsg%' "
                "order by occurred_at desc limit 1",
                (row["chat_id"], row["occurred_at"], *TRIGGER_TYPES),
            ).fetchone()
            if not trig:
                continue
            turns.append({
                "chat_id": str(row["chat_id"]),
                "at": str(row["occurred_at"])[:16],
                "trigger": str(trig["content"]).strip()[:200],
                "reply": str(row["content"]).strip(),
                "is_group": str(row["chat_id"]).endswith("@chatroom"),
            })
    finally:
        con.close()

    if limit and len(turns) > limit:
        step = len(turns) / limit
        turns = [turns[int(i * step)] for i in range(limit)]
    return turns


def build_review_prompt(turn: dict) -> str:
    where = "群聊" if turn["is_group"] else "私聊"
    return (
        f"场景：{where}\n\n"
        f"【当时收到的消息】\n{turn['trigger']}\n\n"
        f"【你准备发出去的回复】\n{turn['reply']}\n\n"
        "这条要发吗？回**一行**，格式固定为：\n"
        "``判断｜理由``\n\n"
        "判断只取三种：\n"
        "- ``OK``      —— 原样发，一个字不改\n"
        "- ``CANCEL``  —— 不发\n"
        "- 或者直接给出改后的正文（只写正文）\n\n"
        "理由不超过 30 字。例：``CANCEL｜太长，像在说教``"
    )


def judge(raw: str, original: str) -> tuple[str, str, str]:
    """判成 (verdict, 新正文, 理由)。verdict ∈ unchanged / cancelled / rewritten / empty。

    理由只是为了**能诊断**（第一版去掉了它，结果 5/5 全撤却不知道为什么）——
    判断本身仍然只看第一行。
    """
    lines = [line for line in str(raw or "").splitlines() if line.strip()]
    if not lines:
        return "empty", "", ""
    first = lines[0].strip()
    reason = ""
    parts = re.split(r"[｜|]", first, 1)
    if len(parts) == 2:
        first, reason = parts[0].strip(), parts[1].strip()
    body: list[str] = [first] + [line.strip() for line in lines[1:]]
    if not first:
        return "empty", "", reason
    head = first.strip("。.!！,， ").upper()
    if head in ("OK", "OKAY", "OKEY"):
        return "unchanged", original, reason
    if head in ("CANCEL", "CANCELED", "CANCELLED", "撤回", "不发"):
        return "cancelled", "", reason
    text = re.sub(r"^(改后|新版本|修订|回复)\s*[:：]\s*", "", "\n".join(body)).strip()
    if text == original.strip():
        return "unchanged", original, reason
    return "rewritten", text, reason

def _pct(n: int, total: int) -> str:
    return "n/a" if total <= 0 else f"{n * 100.0 / total:.1f}%"


def build_report(results: list[dict], model: str, persona_len: int = 0) -> str:
    total = len(results)
    unchanged = [r for r in results if r["verdict"] == "unchanged"]
    cancelled = [r for r in results if r["verdict"] == "cancelled"]
    rewritten = [r for r in results if r["verdict"] == "rewritten"]
    broken = [r for r in results if r["verdict"] == "empty"]
    changed = len(cancelled) + len(rewritten)
    rate = (changed * 100.0 / total) if total else 0.0

    longer = sum(1 for r in rewritten if len(r["new"]) > len(r["reply"]))
    shorter = sum(1 for r in rewritten if len(r["new"]) < len(r["reply"]))
    equal = len(rewritten) - longer - shorter

    if rate < 5:
        verdict = ("**不值得做** —— 改动率 <5%，这扇门基本是仪式，只增加 token 成本。")
    elif rate < 20:
        verdict = ("**边际** —— 改动率 5~20%。先看方向：若集中在「变短 / 少解释」可考虑形态 A；"
                   "若杂乱，先查 prompt。")
    else:
        verdict = ("**值得做** —— 改动率 >20%，继续评估 §2 的 A/B/C 形态。")

    out: list[str] = []
    out.append("# 自审探针读数")
    out.append("")
    out.append(f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M}")
    out.append(f"- 模型：`{model}`　（A 臂不再调模型：基准是**她当时真发出去的那条**）")
    out.append(f"- 自审者拿到的**人设**：{persona_len} 字"
               + ("（没有人设时自审会按通用礼貌判——那是错的）" if persona_len == 0 else ""))
    out.append("- 数据源：`data/yinyue_meta.db` · `terminal_events`（已剔 `m0`/`m1` 测试脏数据）")
    out.append("- 口径见 `scripts/self_review_probe.py` 头注释；本报告**不改任何状态、不发任何消息**")
    out.append("")
    out.append("## 一、总览")
    out.append("")
    out.append("| 项 | 条数 | 占比 |")
    out.append("|---|---|---|")
    out.append(f"| 回合数 | {total} | — |")
    out.append(f"| 未改（OK） | {len(unchanged)} | {_pct(len(unchanged), total)} |")
    out.append(f"| 改写 | {len(rewritten)} | {_pct(len(rewritten), total)} |")
    out.append(f"| 撤回（CANCEL） | {len(cancelled)} | {_pct(len(cancelled), total)} |")
    out.append(f"| **改动率（改写+撤回）** | **{changed}** | **{_pct(changed, total)}** |")
    if broken:
        out.append(f"| 解析失败 | {len(broken)} | {_pct(len(broken), total)} |")
    out.append("")
    out.append("## 二、改写的方向")
    out.append("")
    out.append("| 方向 | 条数 |")
    out.append("|---|---|")
    out.append(f"| 变长 | {longer} |")
    out.append(f"| 变短 | {shorter} |")
    out.append(f"| 等长（换说法） | {equal} |")
    out.append("")
    out.append("## 三、判读")
    out.append("")
    out.append(verdict)
    out.append("")

    def dump(title: str, items: list[dict], n: int, with_new: bool) -> None:
        out.append(title)
        out.append("")
        if not items:
            out.append("_（无）_")
            out.append("")
            return
        for r in items[:n]:
            where = "群" if r["is_group"] else "私"
            r.setdefault("reason", "")
            out.append(f"**`[{r['at']}][{where}]`** 收到：{r['trigger'][:70]}")
            out.append(f"- 原稿（{len(r['reply'])} 字）：{r['reply'][:200]}")
            if r.get("reason"):
                out.append(f"- **自审理由**：{r['reason']}")
            if with_new:
                out.append(f"- 自审后（{len(r['new'])} 字）：{r['new'][:200]}")
            out.append("")
        out.append("")

    dump(f"## 四、改写样例（前 {min(10, len(rewritten))} 条）", rewritten, 10, True)
    dump("## 五、撤回样例", cancelled, 10, False)
    dump("## 六、原样通过的样例（前 3 条）", unchanged, 3, False)
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=30, help="回合数（每回合 1 次 API 调用）")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    turns = pick_turns(args.limit)
    groups = sum(1 for t in turns if t["is_group"])
    print(f"回合：{len(turns)} 条（群聊 {groups} / 私聊 {len(turns) - groups}）")
    if not turns:
        print("没有可用回合。")
        return 1

    from openai import OpenAI

    key = ""
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DEEPSEEK_API_KEY="):
            key = line.split("=", 1)[1].strip()
    if not key:
        print("没找到 DEEPSEEK_API_KEY。")
        return 1
    client = OpenAI(api_key=key, base_url="https://api.deepseek.com/v1")
    persona = load_persona()
    print(f"人设：{'已加载 ' + str(len(persona)) + ' 字' if persona else '读不到'}")

    results: list[dict] = []
    for index, turn in enumerate(turns, 1):
        try:
            resp = client.chat.completions.create(
                model=args.model,
                messages=[
                    {"role": "system", "content": REVIEW_SYSTEM_TEMPLATE.format(
                        persona=(persona or "（读不到人设，按她自己会怎么说话判）")[:1600])},
                    {"role": "user", "content": build_review_prompt(turn)},
                ],
                temperature=0.2,
                max_tokens=400,
            )
            raw = (resp.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001
            raw = f"<调用失败：{exc}>"
        verdict, new_text, reason = judge(raw, turn["reply"])
        results.append({**turn, "verdict": verdict, "new": new_text,
                        "reason": reason, "raw": raw})
        print(f"  {index}/{len(turns)}  {verdict}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else (ROOT / "output" / f"self_review_probe_{stamp}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_report(results, args.model, len(persona)), encoding="utf-8")
    print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
