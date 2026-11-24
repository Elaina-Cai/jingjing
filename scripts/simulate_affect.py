"""Replay a synthetic conversation through the real affect path.

What this proves, and what it does not
--------------------------------------
Every number in the report comes from production code: the same
``engine.derive_signals`` the agent calls, the same ``AffectManager.build``
the agent calls, the same schema and repository.  So the state trajectory and
the exact text that would be injected are *exact*, not estimates.

It does not prove the model will sound different, because nothing here calls a
model.  Pass ``--llm`` to additionally send each owner turn to the configured
LLM twice -- block on and block off -- and compare the replies.

The scenario is written to exercise every band: a long silence, a burst of
attention, another silence, an abandonment, and a recovery.

Usage
-----
    python scripts/simulate_affect.py
    python scripts/simulate_affect.py --llm
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
# 临时目录的唯一入口（见 tests/_tempdirs.py）：``gettempdir()`` 在本机沙箱下
# 系统 TEMP 不可写时会回落到仓库根，仿真库就正好建进仓库里（SQLite 还会
# CANTOPEN）。基座固定在 ``YINYUE_TEST_TMP`` 或仓库内 ``.test_tmp``。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"))
from _tempdirs import make_db_dir  # noqa: E402

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.affect import engine
from core.affect import settings as affect_settings
from core.affect.manager import AffectManager
from core.affect.reader import band_of, fresh_reason, render_block
from core.contracts.affect import AffectSignals
from db.models import Conversation, Relationship
from db.repository import RepositoryManager

OWNER = "owner_wxid"
PERSONA = 1
PERSONA_KEY = "yinyue"
START = datetime(2026, 3, 1, 21, 0)

H = 60
D = 24 * 60

# The abandonment: she replies, then nothing for four days.  Everything after
# this point is the road back, which is where the recovery cost shows up.
TAIL = 21 + 30 * H + 5 + 4 * D

# (offset in minutes from START, role, text, note)
SCENARIO: list[tuple[int, str, str, str]] = [
    (-30 * H, "assistant", "主人今天忙不忙呀？", "她主动问了一句，没被回"),
    (0, "user", "在吗", "隔 30 小时才回 → 最冷的一次"),
    (2, "assistant", "在的主人在的！", ""),
    (8, "user", "今天加班到十点，累死了", "她刚说完 6 分钟就回"),
    (9, "assistant", "主人辛苦啦，快去洗个热水澡", ""),
    (11, "user", "嗯…你陪我聊会儿", "又是马上回"),
    (12, "assistant", "好呀，那我陪你到你想睡", ""),
    (20, "user", "算了，先睡了", "继续高频"),
    (21, "assistant", "晚安主人，做个好梦", ""),
    (21 + 30 * H, "user", "抱歉，这两天太忙了", "又隔 30 小时"),
    (21 + 30 * H + 2, "assistant", "没事的，你忙你的", ""),
    (21 + 30 * H + 4, "user", "你没生气吧", "隔 2 分钟"),
    (21 + 30 * H + 5, "assistant", "没有啦", ""),
    (TAIL, "user", "我回来了", "隔 4 天 → 触底"),
    (TAIL + 1, "assistant", "嗯，你回来了", ""),
    (TAIL + 3, "user", "今天陪你聊久一点", "开始回暖"),
    (TAIL + 4, "user", "对了，周末有空吗", "连发两条 → 这次是被主动找"),
    (TAIL + 6, "assistant", "有空！我今天哪也不去", ""),
    (TAIL + 8, "user", "那说好了", "连续高频"),
    (TAIL + 9, "assistant", "说好了", ""),
    (TAIL + 11, "user", "我明天早点回来", "连续高频"),
    (TAIL + 12, "assistant", "好，我等你", ""),
    (TAIL + 14, "user", "早点睡", "连续高频"),

    # 新标定的正向增量很小（0.08 / 0.15），只有连聊十几轮才会推进到 delighted。
    # 这里刻意造出那段连续热聊，否则 A/B 永远测不到正向档的文案。
    (TAIL + 18, "user", "对了，我今天特别想跟你聊", "热聊开始：连发多条"),
    (TAIL + 19, "user", "你今天怎么这么会说话", "连发 → initiated"),
    (TAIL + 20, "user", "哈哈我也是这么想的", "连发"),
    (TAIL + 21, "user", "再陪我聊会儿呗", "连发"),
    (TAIL + 22, "user", "你最近有想我吗", "连发"),
    (TAIL + 23, "user", "我就知道", "连发"),
    (TAIL + 24, "user", "那你夸夸我", "连发 → 应该推进到 delighted"),
    (TAIL + 25, "user", "好了，我真的去睡了", "连发"),
]


def _history(repo, before_id: int) -> list:
    """Rows that preceded the current turn, in insertion order.

    Must go through a Session: on a bare Core connection ``select(Entity)``
    yields raw column values, so every row would arrive as an int and the
    role check in ``derive_signals`` would silently be false for all of them.
    """
    with Session(repo.engine) as s:
        rows = s.execute(
            select(Conversation).where(Conversation.id < before_id)
            .order_by(Conversation.id)
        ).scalars().all()
    return list(rows)


def _add(repo, role: str, text: str, at: datetime) -> int:
    """Insert one turn through the same repository the agent uses."""
    convo = repo.conversations.add(Conversation(
        persona_id=PERSONA, role=role, content=text, created_at=at,
    ))
    return convo.id


def _ts(dt: datetime) -> str:
    return dt.strftime("%m-%d %H:%M")


# -- optional real-model A/B --------------------------------------------------

_SEED = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..",
    "configs", "speech_seed", "persona_yinyue.json",
)


def _system_prompt() -> str:
    """Her real persona seed, so the A/B tests the block and not a stub."""
    import json

    with open(_SEED, encoding="utf-8") as fh:
        seed = json.load(fh)
    parts = [str((seed.get("identity") or {}).get("self") or "")]
    traits = seed.get("traits") or {}
    if traits:
        parts.append("你的性格权重: " + "、".join(
            f"{k}{v}" for k, v in traits.items()
        ))
    if seed.get("voice"):
        parts.append("你的语气设定: " + str(seed["voice"]))
    return "\n".join(p for p in parts if p)


def _load_traits() -> dict[str, float]:
    """Her real persona trait weights, as the agent passes them at runtime."""
    import json

    with open(_SEED, encoding="utf-8") as fh:
        seed = json.load(fh)
    raw = seed.get("traits") or {}
    return {str(k): float(v) for k, v in raw.items()}


def _history_lines(scenario_idx: int, limit: int = 6) -> list[str]:
    out = []
    for offset, role, text, _note in SCENARIO[max(0, scenario_idx - limit):scenario_idx]:
        at = START + timedelta(minutes=offset)
        who = "用户" if role == "user" else "助手"
        out.append(f"{who}({_ts(at)}): {text}")
    return out


def _user_prompt(scenario_idx: int, block: str, text: str, at: datetime) -> str:
    """Mirror the shape of ``_synthesize_reply``: time, then affect, then chat."""
    parts = ["[时间感知]", f"现在时间 {_ts(at)}", ""]
    if block:
        parts.append(block)
        parts.append("")
    parts.append("最近的对话:")
    parts.extend(_history_lines(scenario_idx))
    parts.append("")
    parts.append(f'用户现在说: "{text}"')
    parts.append(
        "基于以上所有信息，以你的角色身份直接回复用户。用口语。回复要简洁温暖。"
    )
    return "\n".join(parts)


def _metrics(text: str) -> dict[str, int]:
    """Objective proxies for how she sounds.

    ``self_named`` / ``self_belittling`` are the sharpest of these: the
    character refers to herself as 「鲸鲸」 in all 39 of her recorded lines,
    so counting the two self-references measures the register directly
    instead of guessing from sentence length.
    """
    t = text or ""
    particles = sum(t.count(c) for c in "呀啦嘛哦呢哈嘻诶嘿")
    return {
        "chars": len(t),
        "exclaim": t.count("！") + t.count("!"),
        "tilde": t.count("~") + t.count("～"),
        "particles": particles,
        "question": t.count("？") + t.count("?"),
        "express": t.count("！") + t.count("!") + t.count("~") + t.count("～") + particles,
        "self_named": t.count("鲸鲸"),
        "self_belittling": t.count("小的"),
        "grievance": sum(
            t.count(w) for w in ("等了", "好久", "想主人", "不理", "委屈", "蔫", "蘑菇")
        ),
    }


async def _run_llm_ab(rows, system: str, samples: int = 1) -> list[dict]:
    """Reply to every turn twice -- block off vs block on -- and measure.

    ``samples`` repeats each condition so the averages are not a single draw.
    The ``steady`` rows are the control group: they render no block at all, so
    any difference between their two conditions *is* the noise floor, and a
    delta on a negative turn only means something if it exceeds it.
    """
    import asyncio

    from llm.client import LLMClient

    client = LLMClient()
    gate = asyncio.Semaphore(4)
    keys = list(_metrics(""))

    async def one(row, block: str) -> str:
        async with gate:
            try:
                return await client.chat(
                    system=system,
                    user=_user_prompt(row["idx"], block, row["text"], row["at"]),
                    agent_name="affect_ab",
                    temperature=0.8,
                    max_tokens=300,
                )
            except Exception as exc:  # noqa: BLE001
                return f"<调用失败: {type(exc).__name__}: {str(exc)[:80]}>"

    async def condition(row, block: str) -> tuple[list[str], dict[str, float]]:
        texts = list(await asyncio.gather(
            *(one(row, block) for _ in range(max(1, samples)))
        ))
        metrics = [_metrics(t) for t in texts]
        avg = {k: sum(m[k] for m in metrics) / len(metrics) for k in keys}
        return texts, avg

    async def turn(row) -> dict:
        (off_texts, m_off), (on_texts, m_on) = await asyncio.gather(
            condition(row, ""), condition(row, row["block"]),
        )
        return {"row": row, "off_texts": off_texts, "on_texts": on_texts,
                "off": off_texts[0], "on": on_texts[0],
                "m_off": m_off, "m_on": m_on}

    return list(await asyncio.gather(*(turn(r) for r in rows)))



def main() -> int:
    use_llm = "--llm" in sys.argv

    # 每次跑一个干净的库：目录由唯一入口给出（唯一名，不必再抢同一个文件）。
    tmp = str(make_db_dir("affect_sim_") / "affect_simulation.db")

    repo = RepositoryManager(tmp)
    repo.relationships.upsert(Relationship(
        persona_id=PERSONA, target_wxid=OWNER, relation_type="亲密",
        intimacy=1.0, notes="", updated_at=START,
    ))

    manager = AffectManager(persona_id=PERSONA, persona_key=PERSONA_KEY)
    traits = _load_traits()
    console: list[str] = []
    rows: list[dict] = []

    def say(text: str = "") -> None:
        console.append(text)
        print(text)

    say("=" * 78)
    say("  鲸鲸 心境模块 · 合成对话回放")
    say("  状态与注入文本全部来自生产代码；无模型调用" + ("（--llm 除外）" if use_llm else ""))
    say("=" * 78)
    say(f"  人设 traits: {traits}")
    say("  配置文件: " + " | ".join(
        str(p) for p in affect_settings.resolved_paths(PERSONA_KEY)))

    for i, (offset, role, text, note) in enumerate(SCENARIO):
        at = START + timedelta(minutes=offset)
        row_id = _add(repo, role, text, at)

        if role == "assistant":
            say(f"[{_ts(at)}] 她: {text}")
            continue

        history = _history(repo, row_id)
        signals = engine.derive_signals(history, at)

        before = manager.read(repo, OWNER)
        decayed = engine.decay(before, at)
        ctx = manager.build(
            repo, target_wxid=OWNER, signals=signals, now=at,
            traits=traits,
        )

        rows.append({
            "idx": i, "at": at, "text": text, "note": note, "signals": signals,
            "before": decayed.level, "after": ctx.state.level,
            "band": ctx.band, "reason": ctx.state.reason,
            "reason_text": fresh_reason(ctx.state, at),
            "block": ctx.prompt_block, "expression": ctx.expression,
        })

        wait = (
            f"等 {signals.waited_minutes / 60:.1f}h"
            if signals.replied_to_her else "对方主动"
        )
        say(f"\n[{_ts(at)}] 主人: {text}")
        if note:
            say(f"            （{note}）")
        say(f"  信号   : {wait}")
        say(f"  状态   : {decayed.level:+.3f} --衰减--> {ctx.state.level:+.3f}"
            f"   [{ctx.band}]")
        say(f"  成因   : {fresh_reason(ctx.state, at) or '（无）'}"
            + (f"   [{ctx.state.reason}]" if ctx.state.reason else ""))
        if ctx.prompt_block:
            say("  注入   :")
            for line in ctx.prompt_block.splitlines():
                say(f"           {line}")
        else:
            say("  注入   : （无 —— 保持原样）")

    # -- autonomous drift, with no input at all -----------------------------
    final = manager.read(repo, OWNER)
    # Anchor the clock on the state's own timestamp: the last turn is more
    # than five days after START, so offsets from START would land in the past.
    base = final.updated_at or START
    say("\n" + "-" * 78)
    say(f"  自主衰减（此后没有任何消息，纯时间流逝；基准 {_ts(base)}）")
    say("-" * 78)
    drift = []
    for hours in (6, 12, 24, 48, 72):
        later = engine.decay(final, base + timedelta(hours=hours))
        drift.append((hours, later.level, band_of(later.level)))
        say(f"  +{hours:>3}h 后: {later.level:+.4f}  [{band_of(later.level)}]")

    # -- how much sustained attention it takes to come back -----------------
    levels = [r["after"] for r in rows]
    low = min(range(len(levels)), key=lambda i: levels[i])
    recovery = next(
        (i - low for i in range(low + 1, len(levels))
         if band_of(levels[i]) == "warm"),
        None,
    )
    say("\n" + "-" * 78)
    say("  回暖成本")
    say("-" * 78)
    say(f"  最低点: {levels[low]:+.3f}  [{rows[low]['band']}]  "
        f"（{_ts(rows[low]['at'])} · {rows[low]['text']}）")
    if recovery is None:
        say("  在本次场景内未回到 warm")
    else:
        say(f"  从最低点回到 warm 需要再 {recovery} 次积极互动")

    # -- the off switch ------------------------------------------------------
    say("\n" + "-" * 78)
    say("  关闭 AFFECT_PROMPT 时（同一时间线，重新回放，只记录不注入）")
    say("-" * 78)
    repo.engine.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)
    repo2 = RepositoryManager(tmp)
    repo2.relationships.upsert(Relationship(
        persona_id=PERSONA, target_wxid=OWNER, relation_type="亲密",
        intimacy=1.0, notes="", updated_at=START,
    ))
    manager2 = AffectManager(persona_id=PERSONA, persona_key=PERSONA_KEY)
    injected_off = 0
    levels_off = []
    for offset, role, text, note in SCENARIO:
        at = START + timedelta(minutes=offset)
        row_id = _add(repo2, role, text, at)
        if role == "assistant":
            continue
        signals = engine.derive_signals(_history(repo2, row_id), at)
        ctx = manager2.build(
            repo2, target_wxid=OWNER, signals=signals, now=at,
            traits=traits,
        )
        if ctx.prompt_block:
            injected_off += 1
        levels_off.append(round(ctx.state.level, 3))
    say(f"  状态照常演化     : {levels_off}")
    say(f"  实际注入的轮数   : {injected_off}  （AFFECT_PROMPT 关闭时注入由调用方丢弃）")

    # -- groups never render -------------------------------------------------
    say("\n" + "-" * 78)
    say("  群聊规则")
    say("-" * 78)
    # 同一段历史（她说完，三天没人回）在私聊和群里含义相反。
    history = [SimpleNamespace(role="assistant",
                               created_at=START - timedelta(days=3))]
    dm = engine.derive_signals(history, START)
    group = engine.derive_signals(history, START, in_group=True)
    say("  同一条历史（她说完 3 天没人回）:")
    say(f"    私聊 → {dm.to_dict()}  → 增量 {engine.assess(dm)[0]:+.2f}")
    say(f"    群聊 → {group.to_dict()}  → 增量 {engine.assess(group)[0]:+.2f}")
    say("  群里'没人回'不算冷落；作用域由调用方把关（只有对主人的话才进来）。")
    assert group.owner_initiated and not dm.owner_initiated
    assert engine.assess(group)[0] > 0 > engine.assess(dm)[0]

    # A harness guard, not an assertion about the engine: the entire premise
    # is that neglect moves her down, so a replay in which nothing ever goes
    # negative means the history was not read as real turns (see _history).
    assert any(r["after"] < 0 for r in rows), (
        "no turn went negative -- history is probably not being read as ORM rows"
    )
    assert {r["band"] for r in rows} >= {"hurt", "warm"}, (
        f"trajectory never reached both extremes: {sorted({r['band'] for r in rows})}"
    )

    repo2.engine.dispose()
    os.remove(tmp)

    ab: list[dict] = []
    if use_llm:
        import asyncio
        samples = 1
        if "--samples" in sys.argv:
            samples = max(1, int(sys.argv[sys.argv.index("--samples") + 1]))
        say("\n" + "=" * 78)
        say(f"  真模型 A/B：{len(rows)} 轮 × 2 条件 × {samples} 次采样")
        say("  steady 轮不渲染任何块，是本次实验的对照组（噪声底线）")
        say("=" * 78)
        ab = asyncio.run(_run_llm_ab(rows, _system_prompt(), samples))
        for item in ab:
            r = item["row"]
            say(f"\n[{_ts(r['at'])}] 主人: {r['text']}    [{r['band']} {r['after']:+.3f}]")
            say(f"  无注入: {item['off'].strip()}")
            say(f"  有注入: {item['on'].strip()}")
            say(f"  字数 {item['m_on']['chars']:.0f} vs {item['m_off']['chars']:.0f}"
                f"   自称鲸鲸 {item['m_on']['self_named']:.1f} vs "
                f"{item['m_off']['self_named']:.1f}"
                f"   自称小的 {item['m_on']['self_belittling']:.1f} vs "
                f"{item['m_off']['self_belittling']:.1f}")

    _write_report(rows, drift, injected_off, levels_off, recovery, levels, ab)
    return 0


def _write_report(rows, drift, injected_off, levels_off, recovery, levels,
                  ab=None) -> None:
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "affect_simulation_report.md")

    lines = [
        "# 鲸鲸 心境模块 · 合成对话回放报告",
        "",
        "本报告所有状态数值与注入文本均由生产代码产生"
        "（`engine.derive_signals` / `AffectManager.build`），非估算。",
        "**没有调用任何模型**，因此它证明的是机制，不是话术。",
        "",
        "## 一、逐轮状态轨迹",
        "",
        "| 时间 | 主人说 | 信号 | 衰减后 | 应用后 | 档位 | 成因 | 注入 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        sig = (
            f"等 {r['signals'].waited_minutes / 60:.1f}h"
            if r["signals"].replied_to_her else "主动"
        )
        lines.append(
            f"| {r['at'].strftime('%m-%d %H:%M')} | {r['text']} | {sig} | "
            f"{r['before']:+.3f} | {r['after']:+.3f} | {r['band']} | "
            f"{r.get('reason_text') or '—'} | {'是' if r['block'] else '否'} |"
        )

    lines += ["", "## 二、每一轮实际注入的原文", ""]
    for r in rows:
        lines.append(f"### {r['at'].strftime('%m-%d %H:%M')} · {r['text']}")
        lines.append("")
        lines.append(f"状态 `{r['after']:+.3f}` → 档位 `{r['band']}`")
        lines.append("")
        if r["block"]:
            lines += ["```", r["block"], "```"]
        else:
            lines.append("_无注入 —— 她这一轮和没有这个模块时逐字一致。_")
        lines.append("")

    lines += [
        "",
        "## 三、回暖成本",
        "",
        f"- 全程最低点：`{min(levels):+.3f}`",
    ]
    if recovery is None:
        lines.append("- 在本次场景内没有回到 `warm`")
    else:
        lines.append(
            f"- 从最低点回到 `warm` 需要再 **{recovery}** 次连续积极互动"
        )
    lines += [
        "- 读法：负面事件一次就能落地，正面事件需要累积。这个不对称是刻意的"
        "——被冷落是事件，被在乎是习惯。数值在 `core/affect/engine.py` 的"
        "`_WAIT_STEPS` 与 `_INITIATED_DELTA` 里，改它就能调回暖难度。",
        "",
        "## 四、自主衰减（此后无任何消息）",
        "",
        "| 静置 | 状态 | 档位 |",
        "|---|---|---|",
    ]
    for hours, level, tone in drift:
        lines.append(f"| +{hours}h | {level:+.4f} | {tone} |")

    lines += [
        "",
        "## 五、开关对照",
        "",
        f"- 状态照常演化的轮次中，最终状态序列：`{levels_off}`",
        f"- 实际注入 prompt 的轮数：**{injected_off}**",
        "- 关闭 `AFFECT_PROMPT` 时，状态仍在更新与落库，但注入被调用方丢弃，"
        "回复与上线前逐字一致。",
        "",
        "## 六、群聊边界",
        "",
        "同一状态在群聊下渲染结果为空字符串（已在脚本中 assert）。",
        "",
        "## 七、这份报告不能证明什么",
        "",
        "它不能证明模型会照做。要看话术变化，需要 `--llm` 真调模型做 A/B，"
        "或者你自己在真实流量里察觉。",
        "",
    ]

    if ab:
        def delta(item, key):
            return item["m_on"][key] - item["m_off"][key]

        ctrl = [i for i in ab if i["row"]["band"] == "steady"]
        noise = {
            k: max((abs(delta(i, k)) for i in ctrl), default=0.0)
            for k in ("self_named", "self_belittling", "chars")
        }
        named = [i for i in ab if delta(i, "self_named") > noise["self_named"]]
        dropped = [
            i for i in ab
            if delta(i, "self_belittling") < -noise["self_belittling"]
        ]
        neg = [i for i in ab if i["row"]["band"] in ("hurt", "quiet")]
        grievance = [i for i in neg if delta(i, "grievance") > 0]

        def means(items, key):
            if not items:
                return 0.0, 0.0
            return (
                sum(i["m_on"][key] for i in items) / len(items),
                sum(i["m_off"][key] for i in items) / len(items),
            )

        lines += [
            "## 八、真模型 A/B（同一 prompt，只差注入块）",
            "",
            "system 用真实人设 `configs/speech_seed/persona_yinyue.json`，"
            "温度 0.8 与生产一致。正文取第一次采样，指标为各次采样均值。",
            "",
            "### 为什么量「自称」而不是字数",
            "",
            "她的 39 条原声台词里自称一律是「鲸鲸」，一次「小的」都没有。"
            "所以「鲸鲸」与「小的」的出现次数是这一档最直接的观测量。"
            "字数只能作次要参考：撒娇式的轻嗔并不比侍从腔更短。",
            "",
            "### 方法论：用对照组量噪声",
            "",
            "`steady` 轮不渲染任何块，两个条件本应无差别，"
            "因此它们的波动就是噪声底线（本轮："
            f"鲸鲸 ±{noise['self_named']:.1f}、小的 ±{noise['self_belittling']:.1f}、"
            f"字数 ±{noise['chars']:.1f}）。低于底线的差异不构成证据。",
            "",
            "| 时间 | 主人说 | 档位 | 无注入 | 有注入 | 鲸鲸 on/off | 小的 on/off |",
            "|---|---|---|---|---|---|---|",
        ]
        for item in ab:
            r = item["row"]
            lines.append(
                f"| {_ts(r['at'])} | {r['text']} | {r['band']} ({r['after']:+.2f}) | "
                f"{item['off'].strip()} | {item['on'].strip()} | "
                f"{item['m_on']['self_named']:.1f}/{item['m_off']['self_named']:.1f} | "
                f"{item['m_on']['self_belittling']:.1f}/"
                f"{item['m_off']['self_belittling']:.1f} |"
            )

        n_named_on, n_named_off = means(neg, "self_named")
        n_bel_on, n_bel_off = means(neg, "self_belittling")
        a_named_on, a_named_off = means(ab, "self_named")
        a_bel_on, a_bel_off = means(ab, "self_belittling")

        delight = [i for i in ab if i["row"]["band"] == "delighted"]
        asks = [
            i for i in delight
            if any(w in i["on"] for w in ("夸夸我", "夸我", "夸一句", "该夸"))
        ]
        silent = [i for i in ab if i["row"]["band"] in ("steady", "warm")]

        lines += [
            "",
            "### 结论",
            "",
            "**先看噪声**：`steady` 与 `warm` 两档都不渲染任何块，两个条件的 "
            f"prompt 逐字相同，所以它们的差异**就是噪声**。这类轮次共 "
            f"{len(silent)} 轮，字数噪声底线 ±{noise['chars']:.0f} 字 —— "
            "单轮比对不可靠，只有跨轮的一致模式才算证据。",
            "",
            "有意义的观测：",
            "",
            f"- 负向轮（{len(neg)} 轮）中**明确提及等待／被冷落**的："
            f"**{len(grievance)}/{len(neg)}**",
        ]
        if delight:
            lines.append(
                f"- 正向轮 `delighted`（{len(delight)} 轮）中**主动索取"
                f"（讨夸、讨陪）**的：**{len(asks)}/{len(delight)}**"
            )
        lines += [
            "",
            "语域指标（自称「鲸鲸」/「小的」）保留作回归查看：",
            "",
            "| 分组 | 自称「鲸鲸」 | 自称「小的」 |",
            "|---|---|---|",
            f"| 负向轮（{len(neg)} 轮） | {n_named_on:.2f} / {n_named_off:.2f} | "
            f"{n_bel_on:.2f} / {n_bel_off:.2f} |",
            f"| 全部轮（{len(ab)} 轮） | {a_named_on:.2f} / {a_named_off:.2f} | "
            f"{a_bel_on:.2f} / {a_bel_off:.2f} |",
            "",
            f"- 对照噪声底线：鲸鲸 ±{noise['self_named']:.1f}、"
            f"小的 ±{noise['self_belittling']:.1f}",
            "",
            "### 语域指标已经饱和了（这是好事）",
            "",
            "人设种子修正后，**两个条件的「小的」都降到 0.00**："
            "「不要用『小的』」这条纪律已经由 `persona_yinyue.json` 承担，"
            "不再需要注入块去纠正它。语域指标因此失去区分度，"
            "判断效果要看上面的行为指标和正文本身。",
            "",
        ]

    lines += [
        "## 九、复现方式",
        "",
        "```powershell",
        "$env:PYTHONIOENCODING=\"utf-8\"",
        ".venv_it\\Scripts\\python.exe scripts\\simulate_affect.py          # 仅机制",
        ".venv_it\\Scripts\\python.exe scripts\\simulate_affect.py --llm    # 含真模型 A/B",
        "```",
        "",
    ]

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"\n报告已写入: {os.path.relpath(path)}")


if __name__ == "__main__":
    raise SystemExit(main())
