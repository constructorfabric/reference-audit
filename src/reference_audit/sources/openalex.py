"""OpenAlex adapter — broad coverage + the version merge (preprint↔published, T1).

A DOI lookup returns a single Work whose `locations[]` may also list the arXiv landing page; the
normalizer captures those as `version_links` so the matcher can enumerate versions (M5).
"""

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
from reference_audit.sources.normalize import openalex_work_to_record

_BASE = "https://api.openalex.org/works"
# Trim the payload to what we normalize (OpenAlex `select`).
_SELECT = ",".join(
    [
        "id",
        "doi",
        "ids",
        "title",
        "display_name",
        "publication_year",
        "type",
        "authorships",
        "primary_location",
        "locations",
        "biblio",
        "cited_by_count",
    ]
)


class OpenAlexAdapter(SourceAdapter):
    name = "openalex"
    handles = {EntryType.ARTICLE, EntryType.INPROCEEDINGS, EntryType.MISC}
    rate_per_sec = 10.0

    def __init__(self, mailto: str = "reference-audit@example.org", **kw):
        super().__init__(**kw)
        self.mailto = mailto

    def _params(self, **extra: str | int) -> dict:
        return {"mailto": self.mailto, "select": _SELECT, **extra}

    async def lookup_by_id(self, ids: Identifiers) -> SourceQueryResult:
        # Prefer DOI; fall back to arXiv via the DataCite DOI OpenAlex indexes.
        key = None
        if ids.doi:
            key = f"doi:{ids.doi}"
        elif ids.arxiv_id:
            key = f"doi:10.48550/arxiv.{ids.arxiv_id.lower()}"
        if key is None:
            return SourceQueryResult(source=self.name, query_kind="id", records=[])
        try:
            status, data = await get_json(
                self.client, self.rate_limiter, f"{_BASE}/{key}", params=self._params()
            )
        except TransientHTTPError as exc:
            return SourceQueryResult(source=self.name, query_kind="id", error=str(exc))
        if status == 404 or not data:
            return SourceQueryResult(source=self.name, query_kind="id", records=[])
        return SourceQueryResult(
            source=self.name, query_kind="id", records=[openalex_work_to_record(data)]
        )

    async def search_by_metadata(self, entry: BibEntry, limit: int = 10) -> SourceQueryResult:
        if not entry.title:
            return SourceQueryResult(source=self.name, query_kind="metadata", records=[])
        params = self._params(search=entry.title, per_page=limit)
        try:
            _status, data = await get_json(self.client, self.rate_limiter, _BASE, params=params)
        except TransientHTTPError as exc:
            return SourceQueryResult(source=self.name, query_kind="metadata", error=str(exc))
        results = (data or {}).get("results") or []
        records: list[SourceRecord] = [openalex_work_to_record(w) for w in results]
        return SourceQueryResult(source=self.name, query_kind="metadata", records=records)
