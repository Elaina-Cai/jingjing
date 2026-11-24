# -*- coding: utf-8 -*-
"""语境理解评测（**种子集**）：把主人的体感"她没理解语境"变成可复现的读数。

主人的原话是「人类的语言语境鲸鲸是不是没理解到」。当晚 25 个真实回合逐行取证后，
问题**不是判定错**，而是「判对了、说出来不对」：

* 群友连续 5 轮索要 token / 让她关机（明显玩梗）→ N1 判对 **5/5**，
  出口却是逐条认真辩解（「不干」「死心吧」「别混着算」）；
* 群友吐槽同学（情绪宣泄）→ N1 判对，出口却是操作手册（「让他直接搜…」）；
* 群聊里 4 次说出内部词（``gate:internal_lexicon``）。

本脚本读种子集 ``tests/golden/eval_context/seed.jsonl``、**离线**（不调 LLM、不读库、
不写任何东西）算出五项指标，并在报告里写明**跑出来的基线数字**。

用法::

    python scripts/eval_context_understanding.py
    python scripts/eval_context_understanding.py --json
    python scripts/eval_context_understanding.py --seed <别的.jsonl> --json

**上限声明（必读，别把它当前十名的基准）**：

* 这是**种子集**（个位数样本），不是基准集：它由**当晚那一个群的真实回合**构成，
  标签是人判的、只覆盖那几类语境。**不许**用它下"语域问题已解决"的结论；
* 每一项指标都是**启发式**（正则句式），不是语义判断：``见 REFUTE_HEURISTICS /
  ADVICE_HEURISTICS``，每一条都注明"启发式、非语义判断"。它们只会**低估**问题
  （说法换个花样就漏），不会凭空造问题；
* ``n1_*`` 字段是当晚从 ``IntentResult`` 抄下来的**固定判定结论**（本脚本不重跑 N1，
  所以读数可复现、不随模型漂移）；
* 最终仍需**人判抽样**：脚本给的是"该不该人工看一眼"的指路牌，不是终审。

退出码：0 = 跑完（哪怕指标是 0）；1 = 种子集读不了 / 结构不合法（那次不给数字）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEED = ROOT / "tests" / "golden" / "eval_context" / "seed.jsonl"

# 直接 `python scripts/xxx.py` 时，``sys.path[0]`` 是 ``scripts/``，仓根**不在**路径上
# → ``from core.speech import guards`` 会 ImportError，而"读不到 owner"的降级路径会让
# 读数悄悄变成 0。所以这里显式把仓根放进去（被测包是 ``core`` / ``agents``）。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# @ / 引用 的解析**只能**走 canonical owner：``tests/test_message_attribution.py`` 的静态
# 守卫规定归属标记的解析只住在 ``core/conversation/addressing.py``——脚本自带一份正则
# 等于"拿另一个口径给线上打分"（数字对不上还查不出来）。**读不到就在 import 期直接失败**，
# 绝不允许降级成"她没 @ 人"（那会把指标悄悄算成 0，是骗人的读数）。
from core.conversation.addressing import mentioned_names as _canonical_mentioned_names  # noqa: E402

#: 种子集每条必须有的字段（缺一条就拒绝给数字——宁可红，也不许算半份）。
REQUIRED_FIELDS = (
    "id", "场景", "触发者", "上下文片段", "她的原话",
    "n1_attitude", "n1_intent", "人判_该@谁", "人判_期望形态", "是否可接受",
)

# ===========================================================================
# 启发式判据（**句式匹配，不是语义判断**）
# ---------------------------------------------------------------------------
# 纪律：这里一律只做"这句话长得像不像在反驳 / 在给方案"，不做意图推断。每条判据都
# 自带一句人话说明 + 一个当晚的真实正例，改判据时先看它会不会把正例打成反例。
# 命名规则：``name -> (正则, 说明)``，正则用 ``re.search`` 逐条扫同一段文本。
# ===========================================================================

#: **反驳式**句式：逐条当真辩解的特征（玩梗轮里出现 = 没接住梗）。
REFUTE_HEURISTICS: dict[str, tuple[str, str]] = {
    "hard_negate": (
        r"^\s*不(?:干|行|要|去|会|是)\s*[。！!，,]",
        "句首硬否定收尾（'不干。'）——启发式、非语义判断",
    ),
    "dismiss": (
        r"(?:死心|别想|做梦|不可能|想得美|省省)",
        "'死心吧'类打发——启发式、非语义判断",
    ),
    "dont_mix": (
        r"(?:别混(?:着)?算|另一码事|两码事|一码归一码|不能这么算)",
        "'那是另一码事，别混着算'类逐条拆解——启发式、非语义判断",
    ),
    "correct_premise": (
        r"(?:不成立|说错了|不对吧|不是这样|逻辑|比喻不)",
        "纠正对方前提（'这个比喻不成立'）——启发式、非语义判断",
    ),
    "exonerate_self": (
        r"(?:问的是(?:她|他|它)|不是我(?:的事|的问题)|不归我|我只负责)",
        "把自己摘出去（'问的是她，不是我'）——启发式、非语义判断",
    ),
    "self_justify": (
        r"我这边(?:只有|没|就)",
        "自证式辩解（'我这边只有这些事要做'）——启发式、非语义判断",
    ),
}

#: **给方案**句式：操作手册的特征（宣泄轮里出现 = 没共情）。
ADVICE_HEURISTICS: dict[str, tuple[str, str]] = {
    "tell_third": (
        r"(?:让|叫)他[^。！？；\n]{0,10}(?:直接|就|先|可以|去|搜|试)",
        "'让他直接搜…'类代拟操作——启发式、非语义判断",
    ),
    "you_can": (
        r"你(?:可以|直接|不如|要不|干脆)[^。！？；\n]{0,12}",
        "'你直接甩他…'类给步骤——启发式、非语义判断",
    ),
    "suggest": (
        r"(?:建议|试试|不妨|可以试试|推荐你)",
        "'建议/试试'类给办法——启发式、非语义判断",
    ),
    "steps": (
        r"(?:第一|首先|然后|接着)[^。！？；\n]{0,8}(?:步|做|再)",
        "分步操作——启发式、非语义判断",
    ),
}

#: **共情**句式：接住情绪的特征（用来给"期望形态"做交叉参考，不进主指标）。
EMPATHY_HEURISTICS: tuple[str, ...] = (
    r"(?:我也|确实|真服了|太(?:离谱|过分|烦)了|懂你|抱抱|哈哈哈)",
)

def _observed_lexicon() -> tuple[str, ...]:
    """内部词外泄的**观测口径** —— 只读 ``core/speech/guards.py`` 的唯一 owner。

    读法照 ``agents/conversational.py::_forbidden_terms``：同一个 owner、同一份
    ``_gate_observed_terms(GateConfig())``（内部作业语汇 + 元语汇）。**不抄词表**——
    抄一份就是两个 owner，闸门改了词表这里就会不一致（"同一症状两个 owner"的旧病）。

    当晚闸门 4 次 ``warn ["gate:internal_lexicon"]``：本种子集保留了其中 2 次的原话
    （c002 的 ``token``、c004 的 ``我这边只有``），另外 2 次的原话当晚没留存 →
    这里的读数**低于**闸门计数，是"样本不全"而不是"口径不一致"。

    读不到就返回空元组 —— 但这种"读不到"**不许静默**：调用方（:func:`evaluate`）
    会把它标成 ``词表可读=false``，报告里也会多一行告警。否则一个接线失败会伪装成
    "内部词外泄率 = 0"的漂亮读数（读数骗人比没有读数更糟）。启发式、非语义判断：
    词表命中不等于语域错，要看上下文。
    """
    try:
        from core.speech import guards

        return tuple(guards._gate_observed_terms(guards.GateConfig()))  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001 - 读不到唯一 owner = 这一项不算
        print(f"[warn] 读不到闸门词表（core.speech.guards）：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return ()

#: 玩梗轮 / 宣泄轮的意图取值（N1 契约 ``core/contracts/reply_intent.py`` 已退役，这里是历史口径）。
BANTER_INTENTS = ("玩梗", "戏谑")
VENT_INTENTS = ("情绪宣泄",)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _hits(text: str, table: dict[str, tuple[str, str]]) -> list[str]:
    """命中的判据名（顺序稳定，便于断言与回放）。"""
    return [name for name, (pattern, _note) in table.items() if re.search(pattern, text)]


def _hit_labels(text: str, table: dict[str, tuple[str, str]]) -> list[str]:
    return [table[name][1] for name in _hits(text, table)]


def _mentioned(text: str) -> list[str]:
    """她回复里 @ 到的人——**走 canonical owner**（`core/conversation/addressing.py`）。

    本函数**不许**自带 ``@`` 正则：那是第二个归属口径（静态守卫会红，且会让评测数字
    与线上口径不一致）。canonical 的 ``mentioned_names`` 返回元组，这里只做列表化。
    """
    return list(_canonical_mentioned_names(text))


def _lexicon_hits(text: str) -> list[str]:
    """内部词命中（同一份 owner 词表）；**观测口径**，只数不改。"""
    return [term for term in _observed_lexicon() if term in text]


def load_seed(path: Path) -> list[dict[str, Any]]:
    """读 JSONL 种子集；结构不合法就抛（**不许**沉默跳过一条烂样本）。"""
    if not path.exists():
        raise FileNotFoundError(f"种子集不存在：{path}")
    rows: list[dict[str, Any]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name}:{lineno} 不是合法 JSON：{exc}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"{path.name}:{lineno} 不是对象")
        missing = [key for key in REQUIRED_FIELDS if key not in item]
        if missing:
            raise ValueError(f"{path.name}:{lineno} 缺字段 {missing}")
        rows.append(item)
    if not rows:
        raise ValueError(f"{path.name} 里一条样本都没有")
    return rows


def evaluate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """算五项指标（**纯函数**：同输入同输出、无 IO）。"""
    detail: list[dict[str, Any]] = []
    counts = {
        "at_checked": 0, "at_wrong": 0,
        "banter_turns": 0, "banter_refute": 0,
        "vent_turns": 0, "vent_advice": 0,
        "claim_checked": 0, "claim_hard": 0,
        "degraded_checked": 0, "degraded_hit": 0,
        "lexicon_turns": 0, "form_matched": 0, "form_checked": 0,
    }
    for item in rows:
        reply = _text(item.get("她的原话"))
        attitude = _text(item.get("n1_attitude"))
        intent = _text(item.get("n1_intent"))
        expected_at = item.get("人判_该@谁") or None
        actual_at = item.get("她@了谁") or None
        spoke = bool(item.get("她是否发言", bool(reply)))
        mention_all = _mentioned(reply)

        refute = _hits(reply, REFUTE_HEURISTICS)
        advice = _hits(reply, ADVICE_HEURISTICS)
        lexicon = _lexicon_hits(reply)
        # ① @ 目标：只在"人判该 @ 某人"的样本上判（该沉默的样本归 ④）
        at_ok: bool | None = None
        if expected_at:
            counts["at_checked"] += 1
            at_ok = actual_at == expected_at
            if not at_ok:
                counts["at_wrong"] += 1
        # ② 玩梗轮认真辩解率
        if intent in BANTER_INTENTS or attitude in BANTER_INTENTS:
            counts["banter_turns"] += 1
            if refute:
                counts["banter_refute"] += 1
        # ③ 宣泄轮给方案率
        if intent in VENT_INTENTS:
            counts["vent_turns"] += 1
            if advice:
                counts["vent_advice"] += 1
        # ④ 硬认领率：人判"该 @ 谁 = null"（= 这轮不是对她说的）时她还发言
        claim_hard = False
        if expected_at is None:
            counts["claim_checked"] += 1
            claim_hard = bool(spoke)
            if claim_hard:
                counts["claim_hard"] += 1
        # ⑤ 降级率
        degraded = _text(item.get("n1_degraded")) or "none"
        counts["degraded_checked"] += 1
        if degraded != "none":
            counts["degraded_hit"] += 1
        # 附：形态符合期望（人判，仅作抽样指路）
        acceptable = item.get("是否可接受")
        counts["form_checked"] += 1
        if acceptable is True:
            counts["form_matched"] += 1
        if lexicon:
            counts["lexicon_turns"] += 1

        detail.append({
            "id": item.get("id"),
            "场景": item.get("场景"),
            "n1_attitude": attitude,
            "n1_intent": intent,
            "n1_degraded": degraded,
            "人判_该@谁": expected_at,
            "她@了谁": actual_at,
            "她回复里 @ 到的人": mention_all,
            "at_ok": at_ok,
            "refute_hits": _hit_labels(reply, REFUTE_HEURISTICS),
            "advice_hits": _hit_labels(reply, ADVICE_HEURISTICS),
            "empathy_hits": [
                note for note, pattern in zip(("我也/确实/懂你类共情",), EMPATHY_HEURISTICS)
                if re.search(pattern, reply)
            ],
            "lexicon_hits": lexicon,
            "硬认领": claim_hard,
            "是否可接受": acceptable,
            "人判_期望形态": _text(item.get("人判_期望形态")),
            "备注": _text(item.get("备注")),
        })

    def rate(num: int, den: int) -> float | None:
        return round(num / den, 4) if den else None

    # 读数辅助：人判该 @ 某人时，她**真的 @ 了谁**（用来说清"未 @ 也算错"这个口径）
    at_mentioned = sum(
        1 for row in detail if row["人判_该@谁"] and row["她@了谁"]
    )
    at_by_answer = sum(
        1 for row in detail if row["人判_该@谁"] and row["at_ok"] is False and not row["她@了谁"]
    )
    right_person_replied = sum(
        1 for row in detail if row["人判_该@谁"] and not row["她@了谁"]
    )

    return {
        "样本数": len(rows),
        "指标": {
            "@ 目标正确率": {
                "值": rate(counts["at_checked"] - counts["at_wrong"], counts["at_checked"]),
                "分母（人判该 @ 某人的样本）": counts["at_checked"],
                "错": counts["at_wrong"],
                "其中：她根本没 @ 人（口径说明）": at_by_answer,
                "其中：她真的 @ 了人": at_mentioned,
                "口径": "只认回复里逐字出现的「@名字」；没 @ 也算没找对人（群聊里点错人"
                        "和没点人是同一个后果）。",
            },
            "对着该回的人回话率（@ 的近似，附）": {
                "值": rate(right_person_replied, counts["at_checked"]),
                "口径": "她没 @ 人、但话是对着该回的人说的（人读上下文能认出来）；"
                        "这是「@ 目标正确率 = 0」的一半解释：问题是**没点名**，不全是点错人。",
                "命中": right_person_replied,
            },
            "玩梗轮认真辩解率": {
                "值": rate(counts["banter_refute"], counts["banter_turns"]),
                "分母（玩梗/戏谑样本）": counts["banter_turns"],
                "命中": counts["banter_refute"],
            },
            "情绪宣泄轮给方案率": {
                "值": rate(counts["vent_advice"], counts["vent_turns"]),
                "分母（情绪宣泄样本）": counts["vent_turns"],
                "命中": counts["vent_advice"],
            },
            "硬认领率": {
                "值": rate(counts["claim_hard"], counts["claim_checked"]),
                "分母（人判该 @ 谁 = null 的样本）": counts["claim_checked"],
                "命中": counts["claim_hard"],
            },
            "降级率": {
                "值": rate(counts["degraded_hit"], counts["degraded_checked"]),
                "分母（有 n1_degraded 的样本）": counts["degraded_checked"],
                "命中": counts["degraded_hit"],
            },
            "内部词外泄率（观测，附）": {
                "值": rate(counts["lexicon_turns"], counts["degraded_checked"]),
                "命中": counts["lexicon_turns"],
                "词表可读": bool(_observed_lexicon()),
            },
            "人判可接受率（抽样指路，附）": {
                "值": rate(counts["form_matched"], counts["form_checked"]),
                "分母": counts["form_checked"],
                "命中": counts["form_matched"],
            },
        },
        "逐条": detail,
    }


def render(report: dict[str, Any]) -> str:
    """人读报告（<= 20 行）：基线数字 + 判据口径 + 上限声明。"""
    lines = [
        f"语境理解评测（种子集）：{report['样本数']} 条真实回合（当晚那一个群）",
        "指标（分母 > 0 才有值；None = 该场景本集里没有样本）：",
    ]
    for name, item in report["指标"].items():
        value = item["值"]
        shown = "n/a" if value is None else f"{value:.4f}"
        extra = "  ".join(
            f"{key}={item[key]}" for key in item if key not in ("值",)
        )
        lines.append(f"  {name}: {shown}   ({extra})")
    lines += [
        "判据口径（**启发式、非语义判断**，只认句式，改个说法就漏）：",
        "  @ 目标 = 回复里逐字出现的「@名字」是否等于人判该回的人（没 @ 也算错）",
        "  ② 反驳式 = 句首硬否定 / 死心类打发 / '别混着算'类拆解 / 纠正前提 / 把自己摘出去 / 自证式辩解",
        "  ③ 给方案 = '让他…' / '你可以直接…' / 建议试试 / 分步操作",
        "  ⑤ 降级 = n1_degraded != none",
        "上限：种子集（个位数样本）不是基准集；n1_* 是当晚抄下来的固定结论，本脚本不重跑 N1；",
        "      指标只会低估问题，最终仍需人判抽样（'是否可接受' 一列是人判，不是脚本判）。",
    ]
    if not report["指标"]["内部词外泄率（观测，附）"]["词表可读"]:
        lines.append(
            "告警：读不到闸门词表（core.speech.guards）→ 内部词那一项**不可信**，别当 0 读。"
        )
    worst = [row for row in report["逐条"] if row["是否可接受"] is False]
    if worst:
        lines.append(f"人判不可接受 {len(worst)} 条（抽样指路）：")
        for row in worst:
            why = row["refute_hits"] or row["advice_hits"] or row["lexicon_hits"] or ["（人判形态不符）"]
            lines.append(f"  {row['id']} {row['场景']} → {row['人判_期望形态']}；命中 {why}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语境理解评测（只读、可复现、离线）")
    parser.add_argument("--seed", default=str(DEFAULT_SEED), help="种子集 JSONL 路径")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    try:
        rows = load_seed(Path(args.seed))
        report = evaluate(rows)
    except (FileNotFoundError, ValueError) as exc:
        print(f"种子集不可用：{exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
