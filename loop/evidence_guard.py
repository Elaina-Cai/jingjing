"""Backward-compatible imports for shared speech claim consistency."""
from core.speech.claim_consistency import (
    claim_value,
    claims_from_memory_data,
    claims_unverified_promise,
    conflicted_memory_claim,
    guard_memory_text,
    replace_offending_clause,
    safe_conflict_reply,
    safe_corrected_reply,
    safe_uncertain_reply,
    stale_corrected_memory_claim,
    uncertain_memory_claim,
)

__all__ = [
    "claim_value",
    "claims_from_memory_data",
    "claims_unverified_promise",
    "conflicted_memory_claim",
    "guard_memory_text",
    "replace_offending_clause",
    "safe_conflict_reply",
    "safe_corrected_reply",
    "safe_uncertain_reply",
    "stale_corrected_memory_claim",
    "uncertain_memory_claim",
]
