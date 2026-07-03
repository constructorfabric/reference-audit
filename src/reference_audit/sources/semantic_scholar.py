"""Semantic Scholar adapter — strong for CS/ML (zhang2018 CVPR, fu2023 NeurIPS, kumar2024)."""

from __future__ import annotations

from reference_audit.models import (
    BibEntry,
    EntryType,
    Identifiers,
    SourceQueryResult,
    SourceRecord,
)
from reference_audit.sources.base import SourceAdapter
from reference_audit.sources.http import TransientHTTPError, get_json
from reference_audit.sources.normalize import s2_paper_to_record

_BASE = "https://api.semanticscholar.org/graph/v1/paper"
_FIELDS = "title,year,venue,authors,externalIds,citationCount,abstract"


class SemanticScholarAdapter(SourceAdapter):
    name = "semantic_scholar"
    handles = {EntryType.ARTICLE, EntryType.INPROCEEDINGS, EntryType.MISC}
    rate_per_sec = 1.0  # public limit is strict; key (if set) is sent as a header

    def __init__(self, api_key: str | None = None, **kw):
        super().__init__(**kw)
        self.api_key = api_key

    def _headers(self) -> dict | None:
        return {"x-api-key": self.api_key} if self.api_key else None

    async def lookup_by_id(self, ids: Identifiers) -> SourceQueryResult:
        key = None
        if ids.doi:
            key = f"DOI:{ids.doi}"
        elif ids.arxiv_id:
            key = f"ARXIV:{ids.arxiv_id}"
        elif ids.pmid:
            key = f"PMID:{ids.pmid}"
        if key is None:
            return SourceQueryResult(source=self.name, query_kind="id", records=[])
        try:
            status, data = await get_json(
                self.client, self.rate_limiter, f"{_BASE}/{key}",
                params={"fields": _FIELDS}, headers=self._headers(),
            )
        except TransientHTTPError as exc:
            return SourceQueryResult(source=self.name, query_kind="id", error=str(exc))
        if status == 404 or not data:
            return SourceQueryResult(source=self.name, query_kind="id", records=[])
        return SourceQueryResult(
            source=self.name, query_kind="id", records=[s2_paper_to_record(data)]
        )

    async def search_by_metadata(self, entry: BibEntry, limit: int = 10) -> SourceQueryResult:
        if not entry.title:
            return SourceQueryResult(source=self.name, query_kind="metadata", records=[])
        params = {"query": entry.title, "limit": limit, "fields": _FIELDS}
        try:
            _status, data = await get_json(
                self.client, self.rate_limiter, f"{_BASE}/search",
                params=params, headers=self._headers(),
            )
        except TransientHTTPError as exc:
            return SourceQueryResult(source=self.name, query_kind="metadata", error=str(exc))
        papers = (data or {}).get("data") or []
        records: list[SourceRecord] = [s2_paper_to_record(p) for p in papers]
        return SourceQueryResult(source=self.name, query_kind="metadata", records=records)
