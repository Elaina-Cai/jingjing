"""Analyse a real run: what her state was, whether a block was injected, what she said.

This is the live counterpart to ``simulate_affect.py``.  It reads only, and it
re-renders the injected block from the logged fields (level / band /
expression / reason) using the same code the reply path uses, so the prompt
text in the report is the real one rather than a guess.

Usage::

    .venv_it\\Scripts\\python.exe scripts\\analyze_affect_run.py
    .venv_it\\Scripts\\python.exe scripts\\analyze_affect_run.py --last 40
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import temporal  # noqa: E402
from core.affect import settings as affect_settings  # noqa: E402
from core.affect.reader import render_block  # noqa: E402
from core.contracts.affect import AffectState  # noqa: E402

LOG = os.path.join(ROOT, "logs", "yinyue.log")
META = os.path.join(ROOT, "data", "yinyue_meta.db")


def read_observations(limit: int) -> list[dict]:
    out: list[dict] = []
    with io.open(LOG, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if '"event": "affect_observed"' not in line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out[-limit:]


def replies_by_session() -> dict[tuple, list[tuple]]:
    con = sqlite3.connect(f"file:{META}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT session_id, role, content, created_at FROM conversations "
        "ORDER BY id"
    ).fetchall()
    con.close()
    grouped: dict[tuple, list[tuple]] = {}
    for session_id, role, content, created_at in rows:
        grouped.setdefault((str(session_id), role), []).append((created_at, content))
    return grouped


def match_reply(obs: dict, replies: dict) -> str:
    """Nearest assistant reply after the observation.

    Keyed on session_id when the log has it.  Older log lines predate that
    field, so it falls back to matching any assistant reply by time, which is
    still unambiguous to within a few seconds.
    """
    stamp = str(obs.get("timestamp") or "")
    if not stamp:
        return ""
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        moment = temporal.to_naive_local(moment)
    except ValueError:
        return ""

    session = str(obs.get("session_id") or "")
    if session:
        candidates = replies.get((session, "assistant")) or []
    else:
        candidates = [
            row for (sid, role), rows in replies.items()
            if role == "assistant" for row in rows
        ]
        candidates.sort(key=lambda r: str(r[0]))

    for created_at, content in candidates:
        try:
            when = datetime.fromisoformat(str(created_at))
        except ValueError:
            continue
        if when >= moment and (when - moment).total_seconds() < 600:
            return (content or "").strip()
    return ""


def rerender(obs: dict) -> str:
    cfg = affect_settings.load("yinyue")
    trait = cfg.copy.expression_trait
    traits = {trait: float(obs.get("expression") or cfg.copy.expression_default)}
    state = AffectState(
        level=float(obs.get("affect_level") or 0.0),
        updated_at=None,               # TTL always fresh at log time
        reason=str(obs.get("reason") or ""),
    )
    return render_block(
        state, band=str(obs.get("band") or ""),
        now=None, traits=traits, copy=cfg.copy,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--last", type=int, default=20)
    args = parser.parse_args()

    obs = read_observations(args.last)
    if not obs:
        print("日志里还没有 affect_observed。确认调度器已重启（旧进程跑的是旧代码），"
              "并且开过 YINYUE_FEATURE_AFFECT=1。")
        return 1

    replies = replies_by_session()
    total = len(obs)
    injected = sum(1 for o in obs if o.get("injected"))
    # ``rendered`` is only logged by newer builds; older lines fall back to
    # ``injected``.  The two differ whenever YINYUE_FEATURE_AFFECT_PROMPT is
    # off, which is exactly the shadow state: the block is built and logged but
    # thrown away before the prompt, and calling that "注入" would be a lie.
    rendered = sum(1 for o in obs if o.get("rendered", o.get("injected")))
    shadowed = rendered - injected
    print("=" * 78)
    print(f"  最近 {total} 轮：注入 {injected} 轮，"
          f"仅渲染未注入 {shadowed} 轮，静默 {total - rendered} 轮")
    print("=" * 78)

    lines = [
        "# 心境模块 · 真实运行报告",
        "",
        f"- 统计轮数：{total}",
        f"- 实际注入 prompt：{injected}",
        f"- 仅渲染（AFFECT_PROMPT 关闭，未进 prompt）：{shadowed}",
        f"- 静默：{total - rendered}",
        "",
        "| 时间(本地) | 对象 | 状态 | 档位 | 起因 | 注入 | 她实际说的 |",
        "|---|---|---|---|---|---|---|",
    ]

    for o in obs:
        stamp = str(o.get("timestamp") or "")
        try:
            local = datetime.fromisoformat(
                stamp.replace("Z", "+00:00")
            ).astimezone().strftime("%m-%d %H:%M:%S")
        except ValueError:
            local = stamp[:19]
        level = float(o.get("affect_level") or 0.0)
        band = str(o.get("band") or "")
        reason = str(o.get("reason") or "")
        flag = "是" if o.get("injected") else "—"
        reply = match_reply(o, replies).replace("\n", " ")
        if len(reply) > 90:
            reply = reply[:90] + "…"

        print(f"\n[{local}] {o.get('target')}  level={level:+.3f} band={band} "
              f"injected={flag} reason={reason or '-'}")
        if o.get("rendered", o.get("injected")):
            block = rerender(o)
            label = "注入| " if o.get("injected") else "仅渲染| "
            for line in block.splitlines():
                print("      " + label + line)
        print(f"      她说| {reply or '(未能对上会话记录)'}")
        lines.append(
            f"| {local} | {o.get('target')} | {level:+.3f} | {band} | "
            f"{reason or '—'} | {flag} | {reply or '—'} |"
        )

    out = os.path.join(ROOT, "output", "affect_live_report.md")
    with io.open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\n报告已写入: {os.path.relpath(out, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
