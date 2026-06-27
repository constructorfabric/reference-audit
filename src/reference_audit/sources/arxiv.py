"""arXiv adapter — preprint authority (kumar2024automating; preprint side of T1).

Uses the arXiv Atom API. Parsed with stdlib ElementTree (no DTD/entity processing by default).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from reference_audit.models import (
    BibEntry,
    EntryType,
    Identifiers,
    SourceQueryResult,
    SourceRecord,
)
from reference_audit.parsing.identifiers import arxiv_to_doi
from reference_audit.sources.base import SourceAdapter
from reference_audit.sources.http import TransientHTTPError

_BASE = "http://export.arxiv.org/api/query"
_ATOM = "{http://www.w3.org/2005/Atom}"


def _entry_to_record(entry: ET.Element) -> SourceRecord:
    raw_id = (entry.findtext(f"{_ATOM}id") or "").strip()
    arxiv_id = raw_id.rsplit("/abs/", 1)[-1] if "/abs/" in raw_id else raw_id
    arxiv_id = arxiv_id.split("v")[0] if arxiv_id and "v" in arxiv_id.split("/")[-1] else arxiv_id
    title = " ".join((entry.findtext(f"{_ATOM}title") or "").split())
    authors = [
        " ".join((a.findtext(f"{_ATOM}name") or "").split())
        for a in entry.findall(f"{_ATOM}author")
    ]
    authors = [a for a in authors if a]
    published = (entry.findtext(f"{_ATOM}published") or "").strip()
    year = int(published[:4]) if published[:4].isdigit() else None
    return SourceRecord(
        source="arxiv",
        source_native_id=arxiv_id,
        title=title,
        authors=authors,
        year=year,
        ids=Identifiers(arxiv_id=arxiv_id or None, doi=arxiv_to_doi(arxiv_id) if arxiv_id else None),
        is_preprint=True,
        version_links=[raw_id] if raw_id else [],
        raw={"id": raw_id},
    )


class ArxivAdapter(SourceAdapter):
    name = "arxiv"
    handles = {EntryType.MISC, EntryType.ARTICLE, EntryType.INPROCEEDINGS}
    rate_per_sec = 3.0

    async def _query(self, params: dict, query_kind: str) -> SourceQueryResult:
        await self.rate_limiter.acquire()
        try:
            resp = await self.client.get(_BASE, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise TransientHTTPError(f"http {resp.status_code}")
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
        except (httpx.HTTPError, ET.ParseError) as exc:
            return SourceQueryResult(source=self.name, query_kind=query_kind, error=str(exc))
        records = [_entry_to_record(e) for e in root.findall(f"{_ATOM}entry")]
        records = [r for r in records if r.title]
        return SourceQueryResult(source=self.name, query_kind=query_kind, records=records)

    async def lookup_by_id(self, ids: Identifiers) -> SourceQueryResult:
        if not ids.arxiv_id:
            return SourceQueryResult(source=self.name, query_kind="id", records=[])
        return await self._query({"id_list": ids.arxiv_id, "max_results": 1}, "id")

    async def search_by_metadata(self, entry: BibEntry, limit: int = 10) -> SourceQueryResult:
        if not entry.title:
            return SourceQueryResult(source=self.name, query_kind="metadata", records=[])
        params = {"search_query": f'ti:"{entry.title}"', "max_results": limit}
        return await self._query(params, "metadata")
