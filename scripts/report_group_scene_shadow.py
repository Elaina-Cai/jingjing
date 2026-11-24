# -*- coding: utf-8 -*-
"""群聊v2 shadow 收数报告：从 structlog JSONL 日志汇总转正门槛读数。

只读日志，不连库、不改任何状态。覆盖两把新 shadow 的审计事件：

* ``scene_read_shadow``      —— GROUP_SCENE：结构合法率 / 角色把握度 / 分布；
* ``intention_tick_shadow``  —— INTENTION_TICK：到点意图影子读数。

另附**既有线参考读数**（不属于群聊v2 门槛，供"还有哪些 shadow 在跑"一眼看全）：
``output_gate``、``reply_intent_resolved``（这两条生产已 on，只作参考）。

2026-10-22 删两个已无生产者的事件：``reply_decision_shadow``（沉默裁决整块退役）、
``addressee_repaired``（老 @ 守卫退役）。没有发送方的审计事件读出来永远是 0，
留在报告里只会让人以为"这条还在跑"。

用法::

    python scripts/report_group_scene_shadow.py --log logs/yinyue.log
    python scripts/report_group_scene_shadow.py --log logs/yinyue.log --json
    python scripts/report_group_scene_shadow.py --log logs/yinyue.log logs/yinyue.log.1

门槛口径见 ``docs/群聊社会认知_shadow清单.md``（合法率 ≥0.99、群聊回合 ≥200、
wrong_side 人工复核 ≥30、提醒 shadow ≥50 等）。**本脚本只报读数**，转正仍需人工评审。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCENE_EVENT = "scene_read_shadow"
TICK_EVENT = "intention_tick_shadow"
OUTPUT_GATE_EVENT = "output_gate"
REPLY_INTENT_EVENT = "reply_intent_resolved"

SCENE_LEGAL_MIN = 0.99
SCENE_MIN_TURNS = 200
TICK_MIN_DUE = 50


def iter_events(paths: Iterable[str], *, since: str = "") -> Iterable[dict[str, Any]]:
    """逐行读 JSONL；坏行/非 JSON 行跳过（日志里可能混入启动横幅）。"""
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            stamp = str(row.get("timestamp") or "")
            if since and stamp and stamp[:10] < since:
                continue
            yield row


@dataclass
class ShadowStats:
    """按事件族聚合；全部只读、零副作用。"""

    scene_total: int = 0
    scene_ok: int = 0
    scene_present: int = 0
    scene_missing: int = 0
    scene_problems: Counter = field(default_factory=Counter)
    scene_chats: Counter = field(default_factory=Counter)
    scene_genres: Counter = field(default_factory=Counter)
    scene_owner_states: Counter = field(default_factory=Counter)
    scene_mentioned: int = 0
    scene_multi_target: int = 0
    scene_targets: Counter = field(default_factory=Counter)
    scene_updates: int = 0
    scene_with_speaker: int = 0
    scene_speakers: Counter = field(default_factory=Counter)
    scene_role_sum: dict[str, float] = field(default_factory=lambda: {
        "her": 0.0, "owner": 0.0, "others": 0.0, "room": 0.0,
    })



    tick_total: int = 0
    tick_chats: Counter = field(default_factory=Counter)
    tick_due: int = 0
    tick_kinds: Counter = field(default_factory=Counter)

    # 既有线参考（不属于群聊v2 门槛）
    output_gate_total: int = 0
    output_gate_modes: Counter = field(default_factory=Counter)
    reply_intent_total: int = 0

    def add(self, event: dict[str, Any]) -> None:
        name = str(event.get("event") or "")
        if name == SCENE_EVENT:
            self.scene_total += 1
            if event.get("ok") is True:
                self.scene_ok += 1
            present = event.get("present")
            if present is True:
                self.scene_present += 1
            elif present is False:
                self.scene_missing += 1
            problems = event.get("problems") or []
            if isinstance(problems, str):
                problems = [problems]
            for problem in problems:
                self.scene_problems[str(problem)[:120]] += 1
            chat = str(event.get("chat_id") or "")
            if chat:
                self.scene_chats[chat] += 1
            if event.get("ok") is True:
                roles = event.get("roles") or {}
                if isinstance(roles, dict):
                    for key in self.scene_role_sum:
                        try:
                            self.scene_role_sum[key] += float(roles.get(key, 0.0) or 0.0)
                        except (TypeError, ValueError):
                            pass
                genre = str(event.get("genre") or "")
                if genre:
                    self.scene_genres[genre] += 1
                state = str(event.get("owner_state") or "")
                if state:
                    self.scene_owner_states[state] += 1
                if event.get("mentioned_her") is True:
                    self.scene_mentioned += 1
                targets = event.get("targets") or []
                if isinstance(targets, str):
                    targets = [targets]
                if len(targets) > 1:
                    self.scene_multi_target += 1
                for target in targets:
                    self.scene_targets[str(target)] += 1
                try:
                    self.scene_updates += int(event.get("updates_n") or 0)
                except (TypeError, ValueError):
                    pass
                speakers = event.get("speakers") or []
                if isinstance(speakers, str):
                    speakers = [speakers]
                if speakers:
                    self.scene_with_speaker += 1
                for speaker in speakers:
                    self.scene_speakers[str(speaker)] += 1
        elif name == TICK_EVENT:
            self.tick_total += 1
            chat = str(event.get("chat_id") or "")
            if chat:
                self.tick_chats[chat] += 1
            try:
                self.tick_due += int(event.get("due") or 0)
            except (TypeError, ValueError):
                pass
            kinds = event.get("kinds") or []
            if isinstance(kinds, str):
                kinds = [kinds]
            for kind in kinds:
                self.tick_kinds[str(kind)] += 1
        elif name == OUTPUT_GATE_EVENT:
            self.output_gate_total += 1
            self.output_gate_modes[str(event.get("mode") or "?")] += 1
        elif name == REPLY_INTENT_EVENT:
            self.reply_intent_total += 1

    # ------------------------------------------------------------- 读数
    @property
    def scene_legal_rate(self) -> float | None:
        return (self.scene_ok / self.scene_total) if self.scene_total else None

    @property
    def scene_role_means(self) -> dict[str, float]:
        if not self.scene_ok:
            return {key: 0.0 for key in self.scene_role_sum}
        return {
            key: round(value / self.scene_ok, 3)
            for key, value in self.scene_role_sum.items()
        }

    @property
    def verdict(self) -> dict[str, str]:
        """能算的自动读数：pass / fail / insufficient（人工项单列）。"""
        out: dict[str, str] = {}
        rate = self.scene_legal_rate
        out["scene_legal_rate>=0.99"] = (
            "insufficient" if rate is None else ("pass" if rate >= SCENE_LEGAL_MIN else "fail")
        )
        out["scene_turns>=200"] = (
            "pass" if self.scene_total >= SCENE_MIN_TURNS else "insufficient"
        )
        out["tick_due>=50"] = (
            "pass" if self.tick_due >= TICK_MIN_DUE else "insufficient"
        )
        return out

    def as_dict(self) -> dict[str, Any]:
        rate = self.scene_legal_rate
        return {
            "scene": {
                "turns": self.scene_total,
                "ok": self.scene_ok,
                "legal_rate": round(rate, 4) if rate is not None else None,
                "present": self.scene_present,
                "missing": self.scene_missing,
                "invalid": max(0, self.scene_present - self.scene_ok),
                "problems": dict(self.scene_problems.most_common(8)),
                "chats": dict(self.scene_chats.most_common()),
                "role_means": self.scene_role_means,
                "mentioned_her": self.scene_mentioned,
                "multi_target_turns": self.scene_multi_target,
                "targets_top": dict(self.scene_targets.most_common(10)),
                "updates": self.scene_updates,
                "turns_with_speaker": self.scene_with_speaker,
                "speakers_top": dict(self.scene_speakers.most_common(10)),
                "genres": dict(self.scene_genres.most_common()),
                "owner_states": dict(self.scene_owner_states.most_common()),
            },
            "intention_tick": {
                "shadow_rounds": self.tick_total,
                "due": self.tick_due,
                "chats": dict(self.tick_chats.most_common()),
                "kinds": dict(self.tick_kinds.most_common()),
            },
            "existing_lines": {
                "output_gate": {
                    "mode": "on",
                    "hits": self.output_gate_total,
                    "modes": dict(self.output_gate_modes.most_common()),
                },
                "reply_intent": {"mode": "on", "resolved": self.reply_intent_total},
            },
            "verdict": self.verdict,
        }


def render_text(stats: ShadowStats) -> str:
    data = stats.as_dict()
    scene = data["scene"]
    tick = data["intention_tick"]
    lines = [
        "== 群聊v2 shadow 收数报告 ==",
        f"[GROUP_SCENE] 回合={scene['turns']} 合法={scene['ok']} "
        f"合法率={scene['legal_rate']} mentioned_her={scene['mentioned_her']}",
        f"  段出现={scene['present']} 缺段={scene['missing']} 段坏={scene['invalid']}",
        f"  多收件人回合={scene['multi_target_turns']} targets Top={list(scene['targets_top'].items())[:5]}",
        f"  更新条数={scene['updates']} 带说话人的回合={scene['turns_with_speaker']} "
        f"speakers Top={list(scene['speakers_top'].items())[:5]}",
        f"  段坏原因 Top={list(scene['problems'].items())[:3]}",
        f"  角色均值={scene['role_means']}",
        f"  会话 Top={list(scene['chats'].items())[:5]}",
        f"  genre={scene['genres']}",
        f"  owner_state={scene['owner_states']}",
        f"[INTENTION_TICK] shadow 轮次={tick['shadow_rounds']} 到点意图={tick['due']} "
        f"kinds={tick['kinds']}",
        f"OUTPUT_GATE(on) 命中={data['existing_lines']['output_gate']['hits']}；"
        f"REPLY_INTENT(on) 决议={data['existing_lines']['reply_intent']['resolved']}",
        "-- 自动门槛读数（insufficient=样本还不够，不是失败）--",
    ]
    for key, value in data["verdict"].items():
        lines.append(f"  {key}: {value}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="群聊v2 shadow 收数报告（只读日志）")
    parser.add_argument(
        "--log", nargs="+", default=["logs/yinyue.log"],
        help="一个或多个 JSONL 日志路径（默认 logs/yinyue.log）",
    )
    parser.add_argument("--since", default="", help="只看该日期（含）之后的 timestamp，如 2026-10-12")
    parser.add_argument("--json", action="store_true", help="输出 JSON（进评审记录）")
    args = parser.parse_args(argv)

    stats = ShadowStats()
    for event in iter_events(args.log, since=args.since):
        stats.add(event)

    if args.json:
        payload = stats.as_dict()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
