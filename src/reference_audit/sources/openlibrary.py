"""Open Library adapter — books / ISBN backfill (gavrilets2004, mabook).

Matches on title+author (the pilot books have a publisher typo, so publisher is never a feature).
`fetch_editions` additionally enumerates a book's concrete editions (year/publisher/ISBN each) for
the edition-aware book check.
"""

from __future__ import annotations

import re

from reference_audit.models import (
    BibEntry,
    EntryType,
    Identifiers,
    SourceQueryResult,
    SourceRecord,
)
from reference_audit.sources.base import SourceAdapter
from reference_audit.sources.http import TransientHTTPError, get_json
from reference_audit.sources.normalize import (
    openlibrary_doc_to_record,
    openlibrary_edition_to_record,
)

_SEARCH = "https://openlibrary.org/search.json"
_FIELDS = "key,title,author_name,first_publish_year,isbn,edition_count,publisher"

_OL = "https://openlibrary.org"
_EDITIONS_LIMIT = 50  # editions fetched per work
_MAX_WORKS = 6        # Open Library often splits one book across several "work" records


def _fold(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _title_compatible(entry_title: str, work_title: str | None) -> bool:
    """A work is the cited book if the folded titles are equal or one contains the other (Open
    Library appends series subtitles like '(Frontiers in Physics, No. 46)' to some records)."""
    a, b = _fold(entry_title), _fold(work_title)
    return bool(a and b and (a == b or a in b or b in a))


def _author_query(authors: list[str]) -> str | None:
    """A surname to constrain the title search ('Ma, Shang-Keng'/'Shang-Keng Ma' → 'Ma')."""
    if not authors:
        return None
    first = authors[0].strip()
    return (first.split(",")[0] if "," in first else first.split()[-1]).strip() or None


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

    async def fetch_editions(self, entry: BibEntry) -> SourceQueryResult:
        """Every concrete Open Library edition of the cited book (one SourceRecord per edition).

        Works keys come from both the ISBN search (anchors the cited edition) and a title+author
        search (reaches editions Open Library scattered across sibling work records — needed to find
        the latest one). A transport/HTTP failure is surfaced as `error` (never an empty 'no
        editions'), so the caller reports the gap rather than silently passing the book unverified.
        """
        kind = "editions"
        try:
            keys = await self._work_keys(entry)
        except TransientHTTPError as exc:
            return SourceQueryResult(source=self.name, query_kind=kind, error=str(exc))

        records: list[SourceRecord] = []
        seen: set[str] = set()
        errors: list[str] = []
        for key in keys[:_MAX_WORKS]:
            try:
                editions = await self._editions_of_work(key)
            except TransientHTTPError as exc:
                errors.append(f"{key}: {exc}")
                continue
            for rec in editions:
                dedup = rec.ids.isbn13 or rec.source_native_id or f"{rec.year}|{_fold(rec.publisher)}"
                if dedup in seen:
                    continue
                seen.add(dedup)
                records.append(rec)
        if errors and not records:
            return SourceQueryResult(source=self.name, query_kind=kind, error="; ".join(errors))
        return SourceQueryResult(source=self.name, query_kind=kind, records=records)

    async def _work_keys(self, entry: BibEntry) -> list[str]:
        keys: list[str] = []
        if entry.ids.isbn13:
            params: dict[str, str | int] = {"isbn": entry.ids.isbn13, "fields": "key,title", "limit": 5}
            _status, data = await get_json(self.client, self.rate_limiter, _SEARCH, params=params)
            for doc in (data or {}).get("docs") or []:
                key = (doc.get("key") or "").strip()
                if key:
                    keys.append(key)
        if entry.title:
            params = {"title": entry.title, "fields": "key,title", "limit": 10}
            author = _author_query(entry.authors)
            if author:
                params["author"] = author
            _status, data = await get_json(self.client, self.rate_limiter, _SEARCH, params=params)
            for doc in (data or {}).get("docs") or []:
                key = (doc.get("key") or "").strip()
                if key and _title_compatible(entry.title, doc.get("title")):
                    keys.append(key)
        return list(dict.fromkeys(keys))

    async def _editions_of_work(self, work_key: str) -> list[SourceRecord]:
        url = f"{_OL}{work_key}/editions.json"
        _status, data = await get_json(
            self.client, self.rate_limiter, url, params={"limit": _EDITIONS_LIMIT}
        )
        entries = (data or {}).get("entries") or []
        return [openlibrary_edition_to_record(e) for e in entries]
