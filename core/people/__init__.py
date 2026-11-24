"""Platform-neutral person memory services.

No module in this package may import DB, LLM, WeChat/QQ adapters or config.
Adapters implement the ports defined in :mod:`core.people.ports`.
"""

from __future__ import annotations

from core.people.aggregator import ProfileAggregator, RuleBasedProfileAggregator
from core.people.context import PersonContextBuilder
from core.people.extractor import (
    CompositeAsyncObservationExtractor,
    CompositeObservationExtractor,
    ObservationExtractor,
    RuleBasedObservationExtractor,
    SyncObservationExtractorAdapter,
)
from core.people.llm_extractor import LlmObservationExtractor
from core.people.reader import PersonProfileReader
from core.people.service import PersonMemoryService
from core.people.visibility import (
    filter_visible_observations,
    observation_visible_to,
    rank_observations,
)

__all__ = [
    "CompositeAsyncObservationExtractor",
    "CompositeObservationExtractor",
    "LlmObservationExtractor",
    "ObservationExtractor",
    "PersonContextBuilder",
    "PersonMemoryService",
    "PersonProfileReader",
    "ProfileAggregator",
    "RuleBasedObservationExtractor",
    "RuleBasedProfileAggregator",
    "SyncObservationExtractorAdapter",
    "filter_visible_observations",
    "observation_visible_to",
    "rank_observations",
]
