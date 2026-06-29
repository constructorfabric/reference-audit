"""Google Books adapter — broad book coverage where Open Library's strict title search misses.

Open Library's `title=` search is exact-ish: a cited title carrying its subtitle ("Why Nations
Fail: The Origins of Power, Prosperity, and Poverty") returns nothing, and a single off-by-one ISBN
anchors nothing — so a real, famous book can fall through to "not found". Google Books is far more
forgiving (`q=intitle:… inauthor:…`, `q=isbn:…`) and, crucially, a .bib often carries a Google Books
*volume id* in its URL (books.google.…/books?id=…) — the authoritative key for that exact volume,
resolvable with no fuzzy matching at all.

Quota note (the reliability contract): the keyless endpoint shares a global anonymous daily quota
that is routinely exhausted (HTTP 429); an API key gives a per-project quota. Either way a 429/5xx
surfaces as `error` (retry next run) via `get_json`, never a silent "not found".
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
from reference_audit.sources.normalize import google_books_volume_to_record

_BASE = "https://www.googleapis.com/books/v1/volumes"


def _author_surname(authors: list[str]) -> str | None:
    """A surname to constrain the title search ('Acemoglu, D.'/'Daron Acemoglu' → 'Acemoglu')."""
    if not authors:
        return None
    first = authors[0].strip()
    return (first.split(",")[0] if "," in first else first.split()[-1]).strip() or None


class GoogleBooksAdapter(SourceAdapter):
    name = "google_books"
    handles = {EntryType.BOOK, EntryType.INCOLLECTION}
    rate_per_sec = 5.0

    def __init__(self, *, api_key: str | None = None, client=None, limiter=None):
        super().__init__(client=client, limiter=limiter)
        self.api_key = api_key

    def _params(self, **extra: str | int) -> dict:
        params: dict[str, str | int] = dict(extra)
        if self.api_key:
            params["key"] = self.api_key
        return params

    async def lookup_by_id(self, ids: Identifiers) -> SourceQueryResult:
        # The cited volume id is the authoritative key for that exact volume — no fuzzy matching.
        # Otherwise fall back to an ISBN query (one volume).
        try:
            if ids.google_books:
                status, data = await get_json(
                    self.client,
                    self.rate_limiter,
                    f"{_BASE}/{ids.google_books}",
                    params=self._params(),
                )
                if status == 404 or not data:
                    return SourceQueryResult(source=self.name, query_kind="id", records=[])
                return SourceQueryResult(
                    source=self.name,
                    query_kind="id",
                    records=[google_books_volume_to_record(data)],
                )
            if ids.isbn13:
                _status, data = await get_json(
                    self.client,
                    self.rate_limiter,
                    _BASE,
                    params=self._params(q=f"isbn:{ids.isbn13}", maxResults=1),
                )
                items = (data or {}).get("items") or []
                records = [google_books_volume_to_record(v) for v in items[:1]]
                return SourceQueryResult(source=self.name, query_kind="id", records=records)
        except TransientHTTPError as exc:
            return SourceQueryResult(source=self.name, query_kind="id", error=str(exc))
        return SourceQueryResult(source=self.name, query_kind="id", records=[])

    async def search_by_metadata(self, entry: BibEntry, limit: int = 10) -> SourceQueryResult:
        if not entry.title:
            return SourceQueryResult(source=self.name, query_kind="metadata", records=[])
        q = f'intitle:"{entry.title}"'
        surname = _author_surname(entry.authors)
        if surname:
            q += f" inauthor:{surname}"
        try:
            _status, data = await get_json(
                self.client, self.rate_limiter, _BASE, params=self._params(q=q, maxResults=limit)
            )
        except TransientHTTPError as exc:
            return SourceQueryResult(source=self.name, query_kind="metadata", error=str(exc))
        items = (data or {}).get("items") or []
        records: list[SourceRecord] = [google_books_volume_to_record(v) for v in items]
        return SourceQueryResult(source=self.name, query_kind="metadata", records=records)
