"""Shadow-trace reporting for the addressee resolver (P3).

Reads ``addressee_shadow`` JSONL rows and produces the numbers needed before a
canary:

* agreement between legacy routing and the shadow resolver;
* how often the shadow would interject where legacy stayed silent;
* how often the shadow would miss a legacy reply;
* reason/action distributions for tuning.

This is deliberately a pure report over trace data: no DB, no platform, no
behavior changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.conversation.policy import strict_reply_allowed
from safelog import warn

_ACTIONS = ("reply", "observe", "drop", "close", "defer", "allow")


@dataclass
class ShadowReport:
    total: int = 0
    legacy_reply: int = 0
    shadow_reply: int = 0
    agree: int = 0
    disagree: int = 0
    missed_continuation: int = 0
    potential_interjection: int = 0
    shadow_observe: int = 0
    legacy_reply_shadow_observe: int = 0
    # What the strict canary (mention / quote / same-person continuation)
    # would have added on this trace.
    strict_reply: int = 0
    strict_interjection: int = 0
    reason_counts: dict[str, int] = field(default_factory=dict)
    action_matrix: dict[str, int] = field(default_factory=dict)
    # Per-reason error distribution: where interjections and misses live.
    reason_stats: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def agreement_rate(self) -> float:
        return self.agree / self.total if self.total else 0.0

    @property
    def interjection_rate(self) -> float:
        legacy_silent = self.total - self.legacy_reply
        return self.potential_interjection / legacy_silent if legacy_silent else 0.0

    @property
    def miss_rate(self) -> float:
        return self.missed_continuation / self.legacy_reply if self.legacy_reply else 0.0

    @property
    def strict_interjection_rate(self) -> float:
        legacy_silent = self.total - self.legacy_reply
        return self.strict_interjection / legacy_silent if legacy_silent else 0.0

    @property
    def strict_additions(self) -> int:
        """Strict-canary additions: legacy silent + strict-eligible reply.

        The active gate only turns *silent* legacy decisions into replies, so
        this is the actionable count.  ``strict_reply`` also includes rows
        where legacy already replied; those do not change behavior.
        """
        return self.strict_interjection

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "legacy_reply": self.legacy_reply,
            "shadow_reply": self.shadow_reply,
            "agree": self.agree,
            "disagree": self.disagree,
            "agreement_rate": round(self.agreement_rate, 4),
            "missed_continuation": self.missed_continuation,
            "miss_rate": round(self.miss_rate, 4),
            "potential_interjection": self.potential_interjection,
            "interjection_rate": round(self.interjection_rate, 4),
            "shadow_observe": self.shadow_observe,
            "legacy_reply_shadow_observe": self.legacy_reply_shadow_observe,
            "strict_reply": self.strict_reply,
            "strict_additions": self.strict_additions,
            "strict_interjection": self.strict_interjection,
            "strict_interjection_rate": round(self.strict_interjection_rate, 4),
            "action_matrix": dict(sorted(self.action_matrix.items())),
            "reason_counts": dict(sorted(
                self.reason_counts.items(), key=lambda item: (-item[1], item[0])
            )),
            "reason_stats": {
                key: dict(value) for key, value in sorted(self.reason_stats.items())
            },
        }


def _action_of(row: Mapping[str, Any]) -> str:
    shadow = row.get("shadow") or {}
    action = str(shadow.get("action") or "unknown")
    return action if action in _ACTIONS else "unknown"


def summarize_shadow(rows: Iterable[Mapping[str, Any]]) -> ShadowReport:
    report = ShadowReport()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if row.get("kind") != "addressee_shadow":
            continue
        legacy = row.get("legacy") if isinstance(row.get("legacy"), Mapping) else {}
        shadow = row.get("shadow") if isinstance(row.get("shadow"), Mapping) else {}
        legacy_reply = bool(legacy.get("should_reply"))
        action = _action_of(row)
        shadow_reply = action == "reply"
        shadow_observe = action in ("observe", "defer")
        signals = (
            shadow.get("signals")
            if isinstance(shadow.get("signals"), Mapping) else {}
        )
        try:
            score = float(shadow.get("score") or 0.0)
        except (TypeError, ValueError):
            warn("core/conversation/report.py:128 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            score = 0.0

        report.total += 1
        report.legacy_reply += int(legacy_reply)
        report.shadow_reply += int(shadow_reply)
        report.shadow_observe += int(shadow_observe)
        matrix_key = f"legacy={'reply' if legacy_reply else 'silent'}|shadow={action}"
        report.action_matrix[matrix_key] = report.action_matrix.get(matrix_key, 0) + 1
        reason = str(shadow.get("reason") or "unknown")
        report.reason_counts[reason] = report.reason_counts.get(reason, 0) + 1

        stats = report.reason_stats.setdefault(reason, {
            "total": 0,
            "legacy_reply": 0,
            "legacy_silent": 0,
            "interjection": 0,
            "missed": 0,
            "score_sum": 0.0,
        })
        stats["total"] += 1
        stats["legacy_reply"] += int(legacy_reply)
        stats["legacy_silent"] += int(not legacy_reply)
        stats["score_sum"] += score

        if legacy_reply and shadow_reply:
            report.agree += 1
        elif not legacy_reply and not shadow_reply:
            report.agree += 1
        else:
            report.disagree += 1
            if legacy_reply and not shadow_reply:
                report.missed_continuation += 1
                stats["missed"] += 1
            if legacy_reply and shadow_observe:
                report.legacy_reply_shadow_observe += 1
            if not legacy_reply and shadow_reply:
                report.potential_interjection += 1
                stats["interjection"] += 1

        if shadow_reply and strict_reply_allowed(signals, score=score):
            report.strict_reply += 1
            if not legacy_reply:
                report.strict_interjection += 1

    for stats in report.reason_stats.values():
        total = int(stats.get("total", 0) or 0)
        stats["avg_score"] = round(
            float(stats.pop("score_sum", 0.0)) / total, 4,
        ) if total else 0.0
    return report


def load_shadow_rows(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                warn("core/conversation/report.py:193 json.JSONDecodeError", err=repr(__import__("sys").exc_info()[1]))
                continue
    return rows


def load_and_summarize(path: str | Path) -> ShadowReport:
    return summarize_shadow(load_shadow_rows(path))


def render_text(report: ShadowReport) -> str:
    data = report.to_dict()
    lines = [
        "shadow addressee report",
        f"  total                 : {data['total']}",
        f"  legacy reply          : {data['legacy_reply']}",
        f"  shadow reply          : {data['shadow_reply']}",
        f"  agreement rate        : {data['agreement_rate']:.2%}",
        f"  miss rate             : {data['miss_rate']:.2%}",
        f"  interjection rate     : {data['interjection_rate']:.2%}",
        f"  strict-eligible reply : {data['strict_reply']}",
        f"  strict additions      : {data['strict_additions']} "
        f"(legacy-silent, interjection {data['strict_interjection_rate']:.2%})",
        f"  disagreement reasons  : {data['reason_counts']}",
        f"  action matrix         : {data['action_matrix']}",
        "  reason breakdown      :",
    ]
    for reason, stats in data["reason_stats"].items():
        lines.append(
            f"    {reason}: total={stats['total']} "
            f"legacy_reply={stats['legacy_reply']} "
            f"interjection={stats['interjection']} missed={stats['missed']} "
            f"avg_score={stats['avg_score']}"
        )
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - CLI helper
    import sys

    trace_path = sys.argv[1] if len(sys.argv) > 1 else "logs/decision_trace.jsonl"
    print(render_text(load_and_summarize(trace_path)))
