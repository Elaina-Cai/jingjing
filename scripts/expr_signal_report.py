#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""表达层基线读数：她真实说出去的话，现在长什么样（只读）。

为什么要它
----------
判定层失去"安全"辩护之后，它唯一能拿出来的理由是"效果好"——而"效果好"必须可测。
本脚本给的就是**改任何东西之前的那条基线**：以后每动一次表达层，重跑一次，看数字往哪边动。

纪律（照抄 ``scripts/score_two_path_quality.py``）
------------------------------------------------
1. **不编数字**：分母为 0 → 该项输出 ``n/a``，不是 0.0。
2. **不新建表**：唯一数据源是 ``data/yinyue_meta.db`` 的 ``terminal_events``
   （``event_type='reply'``）。``outbound_audit.content_preview`` 是"人读片段"
   （≤80 字），**不得当数据源**。
3. **可复现**：同输入同输出（稳定排序、无随机、无时钟参与计算）。
4. **只读**：不写任何库、不调模型、不联网。

口径（分子/分母逐条写死）
------------------------
==================  ============================================================
有效条数             ``event_type='reply'`` 且正文去空白后非空
**长度**             正文字符数（去首尾空白）；给中位数 / 均值 / P90
**套话率**           分子 = ``guards.detect_canned_phrases`` 命中非空的条数
**残留尾率**         分子 = ``guards.detect_residual_tails`` 命中非空的条数
**问句率**           分子 = 正文含 ``？`` 或 ``?`` 的条数
**括注率**           分子 = 正文含 ``（）`` / ``()`` / ``【】`` / ``[]`` 的条数
~~模板率~~           **已证伪并删除**——见下方「已证伪的口径」。
==================  ============================================================

前两项用表达层止损件的**同一份实现**（同一个纯函数，不是复制一份词表）；
后四项是机械统计——**它们只描述"长什么样"，不判断"好不好"**；
"好不好"要人看 ``--sample`` 抽出来的原文。这是本脚本的边界，不是它的失败。

已证伪的口径（v1 实测，留痕）
--------------------------------
**模板率（跨样本字符 n-gram）删除了。** 两次实测都被高频专名支配：
3-gram 报 73.7%，Top 全是 `Ela`/`ain`/`ina`（@Elaina）；剥掉 @ 改 5-gram 后报 19.3%，
Top 变成 `Anahi`/`ahida`/`nahid`（正文里裸写的 P3）。
它测的是「谁的名字出现得多」，不是「有没有重复的开场/句式」。
**要测模板，请用词表口径**：`detect_canned_phrases`（套话）与 `detect_residual_tails`（残留句式）——
这两项有明确的词表、命中可人工核对，是本脚本保留的两个表达层读数。

用法::
    python scripts/expr_signal_report.py                 # 全量 + 抽 12 条
    python scripts/expr_signal_report.py --sample 30
    python scripts/expr_signal_report.py --since 2026-10-01

归属：表达层基线读数（影子双轨的前置）。只读 + 不调模型。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

META = ROOT / "data" / "yinyue_meta.db"

BRACKETS = ("（", "）", "(", ")", "【", "】", "[", "]")

# 人设明令禁止的自称/姿态词（来源：configs/speech_seed/persona_yinyue.json 的
# identity.self「对主人：…但不用「小的」这类自称，不端茶倒水式伺候，不摆卑微」）。
# 实测因果链：09-23 版 persona prompt（1080 字）**没有**这条禁令 → 09-20~09-25 共
# 49 条（9.2%）命中；09-26 16:19 的改版（1334 字）补上禁令 → 当天归零，此后 14 天 0 条。
# 本指标就是那条因果链的**回归守卫**：数字一旦回升，说明 prompt 又被改回去了。
_PERSONA_BAN_TERMS = ("小的",)

# 背景设定词（来源：seed 的 identity.background）。它的约束是「只在与主人单独相处或主人
# 主动提起时**少量带过**，不主动向外人提」——所以口径必须**分开群聊与私聊**：私聊提到是
# 设计允许的，**群聊提到才是违规**。
#
# 2026-10-10 根因：scripts/reimport_speech_seed.py 原实现只取 identity.self，
# 把 identity.background **整个丢掉**（实测线上 1526 字的 prompt 里含「灵界」=False、
# 「器灵」=False、「本命」=False）——她从来没拿到过那条约束。已修（compose_persona_prompt）。
# 本指标就是修复后的验证位：跑完 reimport 再跑一次，群聊那格应当掉到 0。
_SETTING_TERMS = ("灵界", "器灵", "本命", "圣女", "修仙")

# ---------------------------------------------------------------- 数据（只读）


def connect() -> sqlite3.Connection:
    """只读打开 meta 库。"""
    con = sqlite3.connect(f"file:{META}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


_TEST_PARENT_RE = re.compile(r"^m\d+$")


def load_replies(since: str | None = None) -> tuple[list[dict], int]:
    """她真实发出去的回复 + 被剔除的测试脏数据条数。稳定排序：occurred_at, id。

    剔除规则（v1 实测留痕）：``metadata_json.parent_msg_id`` 形如 ``m0`` / ``m1`` 的行。
    真实微信消息 id 不是这个形状。实测 126 条 ``content='ok'`` **全部**带 ``m0``/``m1``，
    集中在两分钟内成批出现（同一秒 3 条），别的事件类型一条都没有——是联调构造写进
    事件流的，不是她说过的话。不剔会把基线整个带偏：
    长度中位数 41→48、均值 57.7→70.9、P90 125→149、<=8 字占比 20.2%→1.3%。
    """
    sql = (
        "select id, chat_id, occurred_at, content, metadata_json from terminal_events "
        "where event_type='reply' and content is not null and trim(content)<>'' "
    )
    args: list = []
    if since:
        sql += "and occurred_at >= ? "
        args.append(since)
    sql += "order by occurred_at, id"
    con = connect()
    try:
        rows = [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()
    out: list[dict] = []
    skipped = 0
    for r in rows:
        text = str(r["content"] or "").strip()
        if not text:
            continue
        try:
            md = json.loads(r["metadata_json"] or "{}")
        except Exception:
            md = {}
        if _TEST_PARENT_RE.match(str(md.get("parent_msg_id") or "")):
            skipped += 1
            continue
        out.append(
            {
                "id": r["id"],
                "chat_id": str(r["chat_id"] or ""),
                "at": str(r["occurred_at"] or ""),
                "text": text,
                "len": len(text),
            }
        )
    return out, skipped


# ---------------------------------------------------------------- 口径工具


def pct(numerator: int, denominator: int) -> float | None:
    """分母为 0 → None（渲染成 n/a）。绝不拿 0.0 冒充。"""
    if denominator <= 0:
        return None
    return numerator / denominator


def fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def fmt_num(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def median(values: list[int]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2


def mean(values: list[int]) -> float | None:
    return None if not values else sum(values) / len(values)


def p90(values: list[int]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, int(round(0.9 * (len(s) - 1))))
    return float(s[idx])


# ---------------------------------------------------------------- 指标


def rate_of(rows: list[dict], predicate) -> tuple[int, int, float | None]:
    den = len(rows)
    num = sum(1 for r in rows if predicate(r))
    return num, den, pct(num, den)


def canned_hits(text: str) -> list[str]:
    """套话命中。用表达层止损件的同一份实现，不复制词表。"""
    from core.speech.expression_stop import detect_canned_phrases

    return list(detect_canned_phrases(text))


def residual_hits(text: str) -> list[str]:
    """残留尾命中（反将 / 旧账 / 算账）。同一份实现。"""
    from core.speech.expression_stop import detect_residual_tails

    return list(detect_residual_tails(text))


def even_sample(rows: list[dict], n: int) -> list[dict]:
    """确定性等距抽样：同输入同输出，无随机。"""
    if n <= 0 or not rows:
        return []
    if len(rows) <= n:
        return list(rows)
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


LEN_BUCKETS = ((1, 8), (9, 16), (17, 24), (25, 40), (41, 80), (81, 10**9))


def build_report(rows: list[dict], sample_n: int, top_n: int, noise: int = 0) -> str:
    den = len(rows)
    lens = [r["len"] for r in rows]

    canned_n = sum(1 for r in rows if canned_hits(r["text"]))
    residual_n = sum(1 for r in rows if residual_hits(r["text"]))
    question_n = sum(1 for r in rows if ("？" in r["text"] or "?" in r["text"]))
    bracket_n = sum(1 for r in rows if any(b in r["text"] for b in BRACKETS))
    persona_n = sum(1 for r in rows if any(t in r["text"] for t in _PERSONA_BAN_TERMS))
    group_rows = [r for r in rows if r["chat_id"].endswith("@chatroom")]
    priv_rows = [r for r in rows if not r["chat_id"].endswith("@chatroom")]
    setting_grp = sum(1 for r in group_rows if any(t in r["text"] for t in _SETTING_TERMS))
    setting_pri = sum(1 for r in priv_rows if any(t in r["text"] for t in _SETTING_TERMS))

    out: list[str] = []
    out.append("# 表达层基线读数")
    out.append("")
    out.append(f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M}")
    out.append("- 数据源：`data/yinyue_meta.db` · `terminal_events(event_type='reply')`")
    out.append("- 口径：见 `scripts/expr_signal_report.py` 头注释；分母为 0 输出 `n/a`")
    out.append(f"- 已剔除测试脏数据：{noise} 条（`parent_msg_id` 形如 `m0`/`m1`；理由见脚本头注释）")
    out.append("")
    out.append("## 一、总览")
    out.append("")
    out.append("| 指标 | 读数 | 分子/分母 |")
    out.append("|---|---|---|")
    out.append(f"| 有效条数 | {den} | — |")
    out.append(f"| 长度 · 中位数 | {fmt_num(median(lens), 0)} 字 | — |")
    out.append(f"| 长度 · 均值 | {fmt_num(mean(lens))} 字 | — |")
    out.append(f"| 长度 · P90 | {fmt_num(p90(lens), 0)} 字 | — |")
    out.append(f"| **套话率** | **{fmt_pct(pct(canned_n, den))}** | {canned_n}/{den} |")
    out.append(f"| **残留尾率** | **{fmt_pct(pct(residual_n, den))}** | {residual_n}/{den} |")
    out.append(f"| 问句率 | {fmt_pct(pct(question_n, den))} | {question_n}/{den} |")
    out.append(f"| 括注率（动作/旁白痕迹） | {fmt_pct(pct(bracket_n, den))} | {bracket_n}/{den} |")
    out.append(f"| **人设违例率**（{'/'.join(_PERSONA_BAN_TERMS)}） | **{fmt_pct(pct(persona_n, den))}** | {persona_n}/{den} |")
    out.append(f"| 设定叙事 · **群聊**（违规） | {fmt_pct(pct(setting_grp, len(group_rows)))} | {setting_grp}/{len(group_rows)} |")
    out.append(f"| 设定叙事 · 私聊（允许） | {fmt_pct(pct(setting_pri, len(priv_rows)))} | {setting_pri}/{len(priv_rows)} |")
    out.append("")

    out.append("## 二、长度分布")
    out.append("")
    out.append("| 区间 | 条数 | 占比 |")
    out.append("|---|---|---|")
    for lo, hi in LEN_BUCKETS:
        n = sum(1 for v in lens if lo <= v <= hi)
        label = f"{lo}–{hi} 字" if hi < 10**9 else f"{lo}+ 字"
        out.append(f"| {label} | {n} | {fmt_pct(pct(n, den))} |")
    out.append("")

    def section(title: str, items: list[tuple[str, str]]) -> None:
        out.append(f"## {title}")
        out.append("")
        if not items:
            out.append("_（无命中）_")
            out.append("")
            return
        for mark, text in items:
            out.append(f"- `{mark}` {text}")
        out.append("")

    hot_canned = [(",".join(canned_hits(r["text"])), r["text"][:120]) for r in rows if canned_hits(r["text"])]
    section(f"三、套话命中样例（前 {top_n} 条）", hot_canned[:top_n])

    hot_res = [(",".join(residual_hits(r["text"])), r["text"][:120]) for r in rows if residual_hits(r["text"])]
    section(f"四、残留尾命中样例（前 {top_n} 条）", hot_res[:top_n])

    out.append("## 五、人设违例（回归守卫）")
    out.append("")
    out.append("因果链见脚本头注释：禁令补进 prompt 的当天归零。**看的是趋势，不是全时段总量**——")
    out.append("数字回升 = prompt 又被改回去了；查某个时间点之后用 `--since`。")
    out.append("")
    by_day = Counter(r["at"][:10] for r in rows if any(t in r["text"] for t in _PERSONA_BAN_TERMS))
    tot_day = Counter(r["at"][:10] for r in rows)
    if by_day:
        out.append("| 日期 | 命中 | 当日回复 | 占比 |")
        out.append("|---|---|---|---|")
        for day in sorted(by_day):
            out.append(f"| {day} | {by_day[day]} | {tot_day[day]} | "
                       f"{fmt_pct(pct(by_day[day], tot_day[day]))} |")
        last = max(by_day)
        after = [r for r in rows if r["at"][:10] > last]
        out.append("")
        out.append(f"**最后一条命中：{last}。此后 {len(after)} 条回复里命中 "
                   f"{sum(1 for r in after if any(t in r['text'] for t in _PERSONA_BAN_TERMS))} 条。**")
    else:
        out.append("_（全程无命中）_")
    out.append("")
    persona_hits = [(t, r["text"][:120]) for r in rows for t in _PERSONA_BAN_TERMS if t in r["text"]]
    if persona_hits:
        out.append("最早 3 条样例：")
        for mark, text in persona_hits[:3]:
            out.append(f"- `{mark}` {text}")
        out.append("")

    out.append(f"## 六、确定性抽样原文（{sample_n} 条，供人读）")
    out.append("")
    out.append("> 这一节是给人看的：**数字只描述「长什么样」，好不好要读原文。**")
    out.append("")
    for i, r in enumerate(even_sample(rows, sample_n), 1):
        stamp = r["at"][:16].replace("T", " ")
        out.append(f"{i}. `[{stamp}]` {r['text']}")
    out.append("")
    return "\n".join(out)

# ---------------------------------------------------------------- 检查模式
#
# 只对**近期窗口**判定：全时段数字包含已修好的历史问题（「小的」那 49 条发生在
# 2026-09-20~09-25，09-26 补上禁令后归零），拿全时段当门槛会永远红。
# 窗口相对**数据里最新一条回复**算，不是相对 now —— 数据不一定实时。
CHECK_WINDOW_DAYS = 7
CHECK_MIN_SAMPLES = 10        # 少于这个数不判定（样本太少，指标不稳，宁可放过不误报）
CHECK_MAX_MEDIAN_LEN = 80     # 当前基线 48 字；超过它说明又在往「说全说圆」走


def check_recent(rows: list[dict], *, window_days: int = CHECK_WINDOW_DAYS,
                 min_samples: int = CHECK_MIN_SAMPLES) -> tuple[list[str], list[str]]:
    """近窗口判定。返回 (超标项, 正常项)。"""
    from datetime import datetime, timedelta

    from core.speech.guards import detect_roster_records

    if not rows:
        return [], ["无样本"]
    last = max(datetime.fromisoformat(str(r["at"])[:26]) for r in rows)
    since = last - timedelta(days=window_days)
    window = [r for r in rows if datetime.fromisoformat(str(r["at"])[:26]) >= since]
    count = len(window)
    if count < min_samples:
        return [], [f"近 {window_days} 天仅 {count} 条（<{min_samples}），样本不足，跳过"]

    fails: list[str] = []
    oks: list[str] = []
    lens = sorted(r["len"] for r in window)
    median_len = (lens[count // 2] if count % 2
                  else (lens[count // 2 - 1] + lens[count // 2]) / 2)
    (oks if median_len <= CHECK_MAX_MEDIAN_LEN else fails).append(
        f"长度中位数 {median_len:.0f} 字（阈值 <={CHECK_MAX_MEDIAN_LEN}）")

    small = sum(1 for r in window if any(t in r["text"] for t in _PERSONA_BAN_TERMS))
    (oks if small == 0 else fails).append(f"人设违例 {small}/{count}")

    group = [r for r in window if r["chat_id"].endswith("@chatroom")]
    setting = sum(1 for r in group if any(t in r["text"] for t in _SETTING_TERMS))
    (oks if setting == 0 else fails).append(f"设定外扬(群聊) {setting}/{len(group)}")

    roster = sum(1 for r in window if detect_roster_records(r["text"]) >= 2)
    (oks if roster == 0 else fails).append(f"名单形态 {roster}/{count}")

    return fails, oks


# ---------------------------------------------------------------- 入口


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="", help="只统计该时刻之后的回复（字符串比较，如 2026-10-01）")
    ap.add_argument("--sample", type=int, default=12, help="抽样原文条数")
    ap.add_argument("--top", type=int, default=10, help="每类样例条数")
    ap.add_argument("--out", default="output/expr_signal_baseline.md")
    ap.add_argument("--check", action="store_true",
                    help="体检模式：只判近窗口，超标非零退出（给 scripts/doctor.py 调）")
    ap.add_argument("--window-days", type=int, default=CHECK_WINDOW_DAYS)
    args = ap.parse_args()

    rows, noise = load_replies(args.since or None)
    if args.check:
        fails, oks = check_recent(rows, window_days=args.window_days)
        for line in oks:
            print(f"  [ok] {line}")
        for line in fails:
            print(f"  [!!] {line}")
        return 1 if fails else 0
    print(f"有效回复：{len(rows)} 条（已剔除测试脏数据 {noise} 条）")
    if not rows:
        print("没有可统计的回复（分母为 0）；报告不生成。")
        return 1

    report = build_report(rows, args.sample, args.top, noise)
    path = ROOT / args.out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report + "\n", encoding="utf-8")
    print(f"报告：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
