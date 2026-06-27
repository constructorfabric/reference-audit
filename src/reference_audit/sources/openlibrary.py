"""Open Library adapter — books / ISBN backfill (gavrilets2004, mabook).

Matches on title+author (the pilot books have a publisher typo, so publisher is never a feature).
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
from reference_audit.sources.normalize import openlibrary_doc_to_record

_SEARCH = "https://openlibrary.org/search.json"
_FIELDS = "key,title,author_name,first_publish_year,isbn,edition_count"


class OpenLibraryAdapter(SourceAdapter):
    name = "openlibrary"
    handles = {EntryType.BOOK, EntryType.INCOLLECTION}
    rate_per_sec = 5.0

    async def lookup_by_id(self, ids: Identifiers) -> SourceQueryResult:
        if not ids.isbn13:
            return SourceQueryResult(source=self.name, query_kind="id", records=[])
        params = {"isbn": ids.isbn13, "fields": _FIELDS, "limit": 1}
        try:
            _status, data = await get_json(self.client, self.rate_limiter, _SEARCH, params=params)
        except TransientHTTPError as exc:
            return SourceQueryResult(source=self.name, query_kind="id", error=str(exc))
        docs = (data or {}).get("docs") or []
        records = [openlibrary_doc_to_record(d) for d in docs[:1]]
        return SourceQueryResult(source=self.name, query_kind="id", records=records)

    async def search_by_metadata(self, entry: BibEntry, limit: int = 10) -> SourceQueryResult:
        if not entry.title:
            return SourceQueryResult(source=self.name, query_kind="metadata", records=[])
        params: dict[str, str | int] = {"title": entry.title, "fields": _FIELDS, "limit": limit}
        if entry.authors:
            params["author"] = entry.authors[0]
        try:
            _status, data = await get_json(self.client, self.rate_limiter, _SEARCH, params=params)
        except TransientHTTPError as exc:
            return SourceQueryResult(source=self.name, query_kind="metadata", error=str(exc))
        docs = (data or {}).get("docs") or []
        records: list[SourceRecord] = [openlibrary_doc_to_record(d) for d in docs]
        return SourceQueryResult(source=self.name, query_kind="metadata", records=records)
