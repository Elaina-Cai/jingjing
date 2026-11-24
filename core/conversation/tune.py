"""Offline resolver tuning from real shadow traces (P3).

The operator runs the resolver in shadow mode for a while, then replays the
recorded signals here.  Two things happen:

* every trace row's stored signals are re-run through the *exact* resolver
  policy (``AddresseeResolver.decide``), so threshold/weight changes are
  comparable;
* candidate thresholds (and optionally weights) are searched with the real
  disagreement distribution as the guide, under an explicit interjection cap.

Legacy routing is used as the proxy label.  It is not ground truth -- the whole
point of the shadow phase is that legacy is sometimes wrong -- so this tool
only produces a reviewable suggestion file; it never mutates configuration.
"""

from __future__ import annotations

import argparse
import copy
import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core.conversation.policy import strict_reply_allowed
from core.conversation.report import load_shadow_rows, summarize_shadow
from core.conversation.resolver import AddresseeResolver
from core.conversation.signals import AddresseeSignals
from safelog import warn

_THRESHOLD_STEP = 0.05
_WEIGHT_DELTAS = (-0.2, -0.1, -0.05, 0.05, 0.1, 0.2)
_SIGNAL_FIELDS = frozenset(item.name for item in fields(AddresseeSignals))


@dataclass
class TuningMetrics:
    total: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def interjection_rate(self) -> float:
        return self.fp / (self.fp + self.tn) if (self.fp + self.tn) else 0.0

    @property
    def miss_rate(self) -> float:
        return self.fn / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def agreement(self) -> float:
        return (self.tp + self.tn) / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "interjection_rate": round(self.interjection_rate, 4),
            "miss_rate": round(self.miss_rate, 4),
            "agreement": round(self.agreement, 4),
        }


@dataclass
class TuningReport:
    samples: int = 0
    objective: str = "f1"
    max_interjection: float = 0.02
    current_reply_threshold: float = 0.75
    current_open_reply_threshold: float = 0.55
    suggested_reply_threshold: float = 0.75
    suggested_open_reply_threshold: float = 0.55
    current_weights: dict[str, float] = field(default_factory=dict)
    suggested_weights: dict[str, float] = field(default_factory=dict)
    current: TuningMetrics = field(default_factory=TuningMetrics)
    suggested: TuningMetrics = field(default_factory=TuningMetrics)
    strict_reply: int = 0
    strict_interjection: int = 0
    reason_stats: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def improved(self) -> bool:
        return self.suggested.to_dict() != self.current.to_dict()

    @property
    def weight_changes(self) -> dict[str, float]:
        keys = set(self.current_weights) | set(self.suggested_weights)
        return {
            key: round(
                float(self.suggested_weights.get(key, 0.0))
                - float(self.current_weights.get(key, 0.0)), 4,
            )
            for key in sorted(keys)
            if abs(
                float(self.suggested_weights.get(key, 0.0))
                - float(self.current_weights.get(key, 0.0))
            ) > 1e-9
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "objective": self.objective,
            "max_interjection": self.max_interjection,
            "current_thresholds": {
                "reply_threshold": self.current_reply_threshold,
                "open_reply_threshold": self.current_open_reply_threshold,
            },
            "suggested_thresholds": {
                "reply_threshold": self.suggested_reply_threshold,
                "open_reply_threshold": self.suggested_open_reply_threshold,
            },
            "current_metrics": self.current.to_dict(),
            "suggested_metrics": self.suggested.to_dict(),
            "weight_changes": self.weight_changes,
            "suggested_weights": dict(sorted(self.suggested_weights.items())),
            "strict_would_reply": self.strict_reply,
            "strict_would_interject": self.strict_interjection,
            "reason_stats": {
                key: dict(value) for key, value in sorted(self.reason_stats.items())
            },
        }

    def render_text(self) -> str:
        current, suggested = self.current.to_dict(), self.suggested.to_dict()
        lines = [
            "conversation resolver tuning suggestion",
            f"  samples               : {self.samples}",
            f"  interjection cap      : {self.max_interjection:.2%}",
            f"  current  precision    : {current['precision']:.2%}",
            f"  current  recall       : {current['recall']:.2%}",
            f"  current  interjection : {current['interjection_rate']:.2%}",
            f"  suggested precision   : {suggested['precision']:.2%}",
            f"  suggested recall      : {suggested['recall']:.2%}",
            f"  suggested interjection: {suggested['interjection_rate']:.2%}",
            f"  thresholds            : "
            f"{self.current_reply_threshold:.2f}/{self.current_open_reply_threshold:.2f}"
            f" -> {self.suggested_reply_threshold:.2f}/"
            f"{self.suggested_open_reply_threshold:.2f}",
        ]
        if self.weight_changes:
            lines.append(f"  weight changes        : {self.weight_changes}")
        else:
            lines.append("  weight changes        : none")
        return "\n".join(lines)


# --------------------------------------------------------------------- samples
def _sample_from_row(row: Mapping[str, Any]):
    shadow = row.get("shadow")
    if not isinstance(shadow, Mapping):
        return None
    raw_signals = shadow.get("signals")
    if not isinstance(raw_signals, Mapping):
        return None
    kwargs: dict[str, float] = {}
    for key, value in raw_signals.items():
        if key not in _SIGNAL_FIELDS:
            continue
        try:
            kwargs[key] = float(value)
        except (TypeError, ValueError):
            warn("core/conversation/tune.py:183 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            continue
    if not kwargs:
        return None
    legacy = row.get("legacy") if isinstance(row.get("legacy"), Mapping) else {}
    return {
        "signals": AddresseeSignals(**kwargs),
        "legacy_reply": bool(legacy.get("should_reply")),
    }


def extract_samples(rows: Iterable[Mapping[str, Any]]) -> list[Any]:
    samples = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("kind") != "addressee_shadow":
            continue
        sample = _sample_from_row(row)
        if sample is not None:
            samples.append(sample)
    return samples


def _metrics(samples: Sequence[Any], resolver: AddresseeResolver) -> TuningMetrics:
    out = TuningMetrics(total=len(samples))
    for sample in samples:
        predicted = resolver.decide(sample["signals"]).should_reply
        expected = bool(sample["legacy_reply"])
        if predicted and expected:
            out.tp += 1
        elif predicted and not expected:
            out.fp += 1
        elif not predicted and expected:
            out.fn += 1
        else:
            out.tn += 1
    return out


def _objective_value(metrics: TuningMetrics, objective: str) -> float:
    if objective == "recall":
        return metrics.recall
    if objective == "precision":
        return metrics.precision
    if objective == "agreement":
        return metrics.agreement
    return metrics.f1


def _prefer(candidate: TuningMetrics, baseline: TuningMetrics,
            objective: str, epsilon: float = 1e-9) -> bool:
    candidate_value = _objective_value(candidate, objective)
    baseline_value = _objective_value(baseline, objective)
    if candidate_value > baseline_value + epsilon:
        return True
    if candidate_value + epsilon < baseline_value:
        return False
    # Same objective: prefer fewer interjections, then higher recall.
    if candidate.interjection_rate < baseline.interjection_rate - epsilon:
        return True
    if candidate.interjection_rate > baseline.interjection_rate + epsilon:
        return False
    return candidate.recall > baseline.recall + epsilon


def _is_feasible(metrics: TuningMetrics, max_interjection: float) -> bool:
    return metrics.interjection_rate <= max_interjection + 1e-9


def _accept_candidate(candidate: TuningMetrics, baseline: TuningMetrics,
                      objective: str, max_interjection: float) -> bool:
    if not _is_feasible(candidate, max_interjection):
        return False
    if not _is_feasible(baseline, max_interjection):
        # The current production policy already exceeds the cap; accept the
        # first feasible candidate even when it trades some agreement away.
        return True
    return _prefer(candidate, baseline, objective)


def _search_thresholds(
    samples: Sequence[Any],
    weights: Mapping[str, float],
    current_metrics: TuningMetrics,
    *,
    current_reply_threshold: float,
    current_open_reply_threshold: float,
    objective: str,
    max_interjection: float,
) -> tuple[float, float, TuningMetrics]:
    best_rt = float(current_reply_threshold)
    best_ot = float(current_open_reply_threshold)
    best_metrics = current_metrics
    for reply_t in [round(0.45 + i * _THRESHOLD_STEP, 2) for i in range(11)]:
        for open_t in [round(0.30 + i * _THRESHOLD_STEP, 2) for i in range(13)]:
            if open_t > reply_t + 1e-9:
                continue
            resolver = AddresseeResolver(
                weights=dict(weights),
                reply_threshold=reply_t,
                open_reply_threshold=open_t,
                apply_overrides=False,
            )
            metrics = _metrics(samples, resolver)
            if not _accept_candidate(
                metrics, best_metrics, objective, max_interjection,
            ):
                continue
            best_rt, best_ot, best_metrics = reply_t, open_t, metrics
    return best_rt, best_ot, best_metrics


def _search_weights(
    samples: Sequence[Any],
    weights: Mapping[str, float],
    reply_t: float,
    open_t: float,
    current_metrics: TuningMetrics,
    *,
    objective: str,
    max_interjection: float,
    passes: int = 2,
) -> tuple[dict[str, float], TuningMetrics]:
    best_weights = dict(weights)
    best_metrics = current_metrics
    for _ in range(max(0, passes)):
        changed = False
        for key in sorted(best_weights):
            for delta in _WEIGHT_DELTAS:
                candidate_weights = copy.deepcopy(best_weights)
                candidate_weights[key] = round(
                    max(-1.0, min(1.0, candidate_weights[key] + delta)), 4,
                )
                if candidate_weights[key] == best_weights[key]:
                    continue
                resolver = AddresseeResolver(
                    weights=candidate_weights,
                    reply_threshold=reply_t,
                    open_reply_threshold=open_t,
                    apply_overrides=False,
                )
                metrics = _metrics(samples, resolver)
                if not _accept_candidate(
                    metrics, best_metrics, objective, max_interjection,
                ):
                    continue
                best_weights = candidate_weights
                best_metrics = metrics
                changed = True
                break
        if not changed:
            break
    return best_weights, best_metrics


def analyze(
    rows: Iterable[Mapping[str, Any]],
    *,
    objective: str = "f1",
    max_interjection: float = 0.02,
    search_weights: bool = True,
    weight_passes: int = 2,
) -> TuningReport:
    rows = list(rows)
    samples = extract_samples(rows)
    summary = summarize_shadow(rows)

    base = AddresseeResolver()
    report = TuningReport(
        samples=len(samples),
        objective=objective,
        max_interjection=float(max_interjection),
        current_reply_threshold=base.reply_threshold,
        current_open_reply_threshold=base.open_reply_threshold,
        suggested_reply_threshold=base.reply_threshold,
        suggested_open_reply_threshold=base.open_reply_threshold,
        current_weights=dict(base.weights),
        suggested_weights=dict(base.weights),
        strict_reply=summary.strict_reply,
        strict_interjection=summary.strict_interjection,
        reason_stats=summary.to_dict()["reason_stats"],
    )
    if not samples:
        return report

    report.current = _metrics(samples, base)
    reply_t, open_t, threshold_metrics = _search_thresholds(
        samples, base.weights, report.current,
        current_reply_threshold=base.reply_threshold,
        current_open_reply_threshold=base.open_reply_threshold,
        objective=objective, max_interjection=max_interjection,
    )
    report.suggested_reply_threshold = reply_t
    report.suggested_open_reply_threshold = open_t
    report.suggested = threshold_metrics

    if search_weights:
        suggested_weights, weight_metrics = _search_weights(
            samples, base.weights, reply_t, open_t, report.suggested,
            objective=objective, max_interjection=max_interjection,
            passes=weight_passes,
        )
        report.suggested_weights = suggested_weights
        report.suggested = weight_metrics
    else:
        report.suggested_weights = dict(base.weights)

    return report


def render_env(report: TuningReport) -> str:
    """Render the suggestion as environment variables for a safe rollout.

    The operator appends these to ``.env`` (or exports them in PowerShell),
    restarts the scheduler, and can revert by deleting the lines.  The tuning
    file variable is not required when the explicit variables are used.
    """
    weights = json.dumps(
        dict(sorted(report.suggested_weights.items())),
        ensure_ascii=False,
        sort_keys=True,
    )
    return "\n".join([
        "# generated by core.conversation.tune -- append to .env or export",
        f"YINYUE_CONVERSATION_WEIGHTS={weights}",
        "YINYUE_CONVERSATION_REPLY_THRESHOLD="
        f"{report.suggested_reply_threshold}",
        "YINYUE_CONVERSATION_OPEN_REPLY_THRESHOLD="
        f"{report.suggested_open_reply_threshold}",
        "",
    ])


def analyze_trace(
    path: str | Path,
    *,
    objective: str = "f1",
    max_interjection: float = 0.02,
    search_weights: bool = True,
    weight_passes: int = 2,
) -> TuningReport:
    return analyze(
        load_shadow_rows(path),
        objective=objective,
        max_interjection=max_interjection,
        search_weights=search_weights,
        weight_passes=weight_passes,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", nargs="?", default="logs/decision_trace.jsonl")
    parser.add_argument("--out", default="", help="write the suggestion as JSON")
    parser.add_argument("--env-out", default="",
                        help="write suggested weights/thresholds as .env lines")
    parser.add_argument("--objective", default="f1",
                        choices=("f1", "recall", "precision", "agreement"))
    parser.add_argument("--max-interjection", type=float, default=0.02,
                        help="reject candidates above this false-interjection rate")
    parser.add_argument("--no-weight-search", action="store_true")
    parser.add_argument("--weight-passes", type=int, default=2)
    args = parser.parse_args()

    report = analyze_trace(
        args.trace,
        objective=args.objective,
        max_interjection=args.max_interjection,
        search_weights=not args.no_weight_search,
        weight_passes=args.weight_passes,
    )
    print(report.render_text())
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"suggestion written: {target}")
    if args.env_out:
        target = Path(args.env_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_env(report), encoding="utf-8")
        print(f"env lines written: {target}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI helper
    raise SystemExit(main())
