"""Crossref adapter — gold DOI metadata + the relation (version) graph.

Polite pool via `mailto`. `lookup_by_id` resolves a DOI directly; `search_by_metadata` does a
bibliographic + author query and returns the whole top-k (central matching disambiguates).
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
from reference_audit.sources.normalize import crossref_item_to_record

_BASE = "https://api.crossref.org/works"


class CrossrefAdapter(SourceAdapter):
    name = "crossref"
    handles = {
        EntryType.ARTICLE,
        EntryType.INPROCEEDINGS,
        EntryType.BOOK,
        EntryType.INCOLLECTION,
    }
    rate_per_sec = 10.0

    def __init__(self, mailto: str = "reference-audit@example.org", **kw):
        super().__init__(**kw)
        self.mailto = mailto

    async def lookup_by_id(self, ids: Identifiers) -> SourceQueryResult:
        if not ids.doi:
            return SourceQueryResult(source=self.name, query_kind="id", records=[])
        # Crossref takes the raw DOI in the path (literal slashes); httpx encodes the rest.
        url = f"{_BASE}/{ids.doi}"
        try:
            status, data = await get_json(
                self.client, self.rate_limiter, url, params={"mailto": self.mailto}
            )
        except TransientHTTPError as exc:
            return SourceQueryResult(source=self.name, query_kind="id", error=str(exc))
        if status == 404 or not data:
            return SourceQueryResult(source=self.name, query_kind="id", records=[])
        item = data.get("message") or {}
        return SourceQueryResult(
            source=self.name, query_kind="id", records=[crossref_item_to_record(item)]
        )

    async def search_by_metadata(self, entry: BibEntry, limit: int = 10) -> SourceQueryResult:
        params: dict[str, str | int] = {"rows": limit, "mailto": self.mailto, "select": _SELECT}
        if entry.title:
            params["query.bibliographic"] = entry.title
        if entry.authors:
            params["query.author"] = " ".join(entry.authors)
        try:
            status, data = await get_json(self.client, self.rate_limiter, _BASE, params=params)
        except TransientHTTPError as exc:
            return SourceQueryResult(source=self.name, query_kind="metadata", error=str(exc))
        items = ((data or {}).get("message") or {}).get("items") or []
        records: list[SourceRecord] = [crossref_item_to_record(it) for it in items]
        return SourceQueryResult(source=self.name, query_kind="metadata", records=records)


_SELECT = ",".join(
    [
        "DOI",
        "title",
        "author",
        "container-title",
        "issued",
        "published",
        "page",
        "type",
        "ISBN",
        "relation",
        "is-referenced-by-count",
    ]
)
