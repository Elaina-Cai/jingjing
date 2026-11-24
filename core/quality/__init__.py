"""Layer-2 quality feedback collection (see core.quality.feedback)."""

from core.quality.feedback import (
    classify_correction,
    detect_signals,
    is_repeat,
    record_feedback_signals,
)

__all__ = [
    "classify_correction",
    "detect_signals",
    "is_repeat",
    "record_feedback_signals",
]
