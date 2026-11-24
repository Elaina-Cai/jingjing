"""Evidence indexing service for the refactor.

The service is storage-agnostic: it accepts a repository object, which keeps
``core`` independent from the concrete SQLAlchemy implementation.
"""

from __future__ import annotations

from core.evidence.indexer import EvidenceIndexer

__all__ = ["EvidenceIndexer"]
