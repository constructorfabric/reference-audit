"""DBLP adapter — authoritative coverage of the premier CS/ML venues.

NeurIPS, ICLR, and ICML/PMLR mint **no DOI** and are thinly or ambiguously indexed by the
article-centric aggregators (Crossref/OpenAlex/S2), so a real conference paper cited only by its
proceedings URL can otherwise fall through to "unable to verify". DBLP indexes exactly these venues
with exact titles, full author lists, year, venue, and the proceedings landing page (`ee`) — the
record needed to confirm such an entry.

This is a metadata-search recall source (there is no DBLP-native id in a typical `.bib`); a DOI it
carries is still surfaced for backfill. A 429/5xx surfaces as `error` (retry next run) via
`get_json`, never a silent "not found" — DBLP rate-limits aggressive callers, so the adapter is
deliberately polite.
"""

from __future__ import annotations

from reference_audit.models import (
    BibEntry,
    EntryType,
    SourceQueryResult,
    SourceRecord,
)
from reference_audit.sources.base import SourceAdapter
from reference_audit.sources.http import TransientHTTPError, get_json
from reference_audit.sources.normalize import dblp_hit_to_record

_BASE = "https://dblp.org/search/publ/api"


class DblpAdapter(SourceAdapter):
    name = "dblp"
    handles = {EntryType.ARTICLE, EntryType.INPROCEEDINGS, EntryType.MISC}
    rate_per_sec = 2.0

    async def search_by_metadata(self, entry: BibEntry, limit: int = 10) -> SourceQueryResult:
        if not entry.title:
            return SourceQueryResult(source=self.name, query_kind="metadata", records=[])
        params = {"q": entry.title, "format": "json", "h": limit}
        try:
            _status, data = await get_json(self.client, self.rate_limiter, _BASE, params=params)
        except TransientHTTPError as exc:
            return SourceQueryResult(source=self.name, query_kind="metadata", error=str(exc))
        hits = (((data or {}).get("result") or {}).get("hits") or {}).get("hit") or []
        # DBLP collapses a single-element array to a bare object.
        if isinstance(hits, dict):
            hits = [hits]
        records: list[SourceRecord] = [dblp_hit_to_record(h) for h in hits]
        return SourceQueryResult(source=self.name, query_kind="metadata", records=records)
