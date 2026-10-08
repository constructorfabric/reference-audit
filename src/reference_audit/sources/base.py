"""SourceAdapter ABC — the modular adapter contract."""

from __future__ import annotations

from abc import ABC

from reference_audit.models import BibEntry, EntryType, Identifiers, SourceQueryResult
from reference_audit.sources.http import MonotonicRateLimiter, new_client


class SourceAdapter(ABC):
    """One scholarly source.

    `lookup_by_id` is privileged precision (≤1 record from a DOI/ISBN/arXiv endpoint); its result
    is still scored downstream so a hallucinated-but-resolving id fails the title+author gate.
    `search_by_metadata` is recall (top-k); it returns the WHOLE candidate list — central matching
    decides, never the adapter.
    """

    name: str = "source"
    handles: set[EntryType] = set()
    rate_per_sec: float = 5.0
    # Where the data comes from: the public API ("api"), or a local mirror of the same database
    # ("clickhouse"). Two backends of one source share `name` (routing, field priorities and reports
    # are about the database, not the transport), so cached responses are keyed by `cache_source`.
    backend: str = "api"

    def __init__(self, *, client=None, limiter: MonotonicRateLimiter | None = None):
        self.client = client or new_client()
        self.rate_limiter = limiter or MonotonicRateLimiter(self.rate_per_sec)

    @property
    def cache_source(self) -> str:
        """The source key this adapter's responses are cached under — never shared across backends."""
        return self.name if self.backend == "api" else f"{self.name}@{self.backend}"

    async def preflight(self) -> None:
        """Raise if this source cannot be queried at all, before any entry is audited (no-op here)."""

    async def lookup_by_id(self, ids: Identifiers) -> SourceQueryResult:  # noqa: ARG002
        return SourceQueryResult(source=self.name, query_kind="id", records=[])

    async def search_by_metadata(self, entry: BibEntry, limit: int = 10) -> SourceQueryResult:  # noqa: ARG002
        return SourceQueryResult(source=self.name, query_kind="metadata", records=[])

    async def aclose(self) -> None:
        await self.client.aclose()
