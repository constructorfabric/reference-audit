"""DBLP adapter — authoritative coverage of the premier CS/ML venues.

NeurIPS, ICLR, and ICML/PMLR mint **no DOI** and are thinly or ambiguously indexed by the
article-centric aggregators (Crossref/OpenAlex/S2), so a real conference paper cited only by its
proceedings URL can otherwise fall through to "unable to verify". DBLP indexes exactly these venues
with exact titles, full author lists, year, venue, and the proceedings landing page — the record
needed to confirm such an entry.

DBLP is queried through its **SPARQL endpoint** (`sparql.dblp.org`, a QLever instance), not the
`/search/publ/api` search API. The search API and its mirrors now answer automated clients with a
bot-challenge HTML page instead of JSON, which made every DBLP query an error and left every
unmatched entry unresolved. The SPARQL endpoint serves the same records as JSON.

A search is two requests:
1. A title search over QLever's word index. A publication matches when its title contains **every**
   searchable word of the cited title (the search API also required every word). Hits are ordered
   shortest-title-first, which puts the exact title ahead of the many longer titles that quote it
   ("Attention Is All You Need" vs "Not All Attention Is All You Need").
2. The properties and ordered author signatures of the top `limit` hits.

The records then go to central matching, which decides.

This is a metadata-search recall source (there is no DBLP-native id in a typical `.bib`); a DOI it
carries is still surfaced for backfill. A 429/5xx, a non-JSON body, or a title with no searchable
word surfaces as `error` (retry next run), never a silent "not found".
"""

from __future__ import annotations

import re

from reference_audit.models import (
    BibEntry,
    EntryType,
    SourceQueryResult,
    SourceRecord,
)
from reference_audit.sources.base import SourceAdapter
from reference_audit.sources.http import TransientHTTPError, get_json
from reference_audit.sources.normalize import dblp_sparql_to_records
from reference_audit.sources.titlewords import title_search_words

_ENDPOINT = "https://sparql.dblp.org/sparql"

_PREFIXES = """\
PREFIX dblp: <https://dblp.org/rdf/schema#>
PREFIX ql: <http://qlever.cs.uni-freiburg.de/builtin-functions/>
"""

# Step 1: the candidate publications. Kept separate from step 2 because QLever plans the two joined
# into one query poorly (seconds instead of tens of milliseconds).
_SEARCH = _PREFIXES + """\
SELECT DISTINCT ?publ ?title WHERE {{
  ?publ dblp:title ?title .
  ?text ql:contains-entity ?title .
  ?text ql:contains-word "{words}" .
}}
ORDER BY STRLEN(?title) LIMIT {limit}
"""

# Step 2: the properties of those publications, one UNION branch per property so no branch multiplies
# another's rows. The normalizer folds the rows back into one record per publication.
_DETAILS = _PREFIXES + """\
SELECT ?publ ?type ?year ?venue ?pages ?doi ?ee ?ordinal ?name WHERE {{
  VALUES ?publ {{ {iris} }}
  {{ ?publ a ?type }}
  UNION {{ ?publ dblp:yearOfPublication ?year }}
  UNION {{ ?publ dblp:publishedIn ?venue }}
  UNION {{ ?publ dblp:pagination ?pages }}
  UNION {{ ?publ dblp:doi ?doi }}
  UNION {{ ?publ dblp:primaryDocumentPage ?ee }}
  UNION {{
    ?publ dblp:hasSignature ?sig .
    ?sig a dblp:AuthorSignature ; dblp:signatureOrdinal ?ordinal ; dblp:signatureDblpName ?name .
  }}
}}
"""

# A publication IRI as the endpoint returns it; anything else is not interpolated into a query.
_PUBL_IRI_RE = re.compile(r"^https://dblp\.org/rec/[^\s<>\"{}|\\^`]+$")
_ACCEPT = {"Accept": "application/sparql-results+json"}


def build_search_query(words: list[str], limit: int) -> str:
    # `words` are [a-z0-9]+ only (title_search_words), so they are safe inside the string literal.
    return _SEARCH.format(words=" ".join(words), limit=int(limit))


def build_details_query(iris: list[str]) -> str:
    return _DETAILS.format(iris=" ".join(f"<{iri}>" for iri in iris))


class _UnexpectedResponse(Exception):
    """A SPARQL response without the result shape the query asked for."""


def _bindings(data: dict | None) -> list[dict]:
    bindings = ((data or {}).get("results") or {}).get("bindings")
    if not isinstance(bindings, list):
        raise _UnexpectedResponse(f"unexpected SPARQL response: {str(data)[:200]}")
    return bindings


class DblpAdapter(SourceAdapter):
    name = "dblp"
    handles = {EntryType.ARTICLE, EntryType.INPROCEEDINGS, EntryType.MISC}
    rate_per_sec = 2.0

    async def search_by_metadata(self, entry: BibEntry, limit: int = 10) -> SourceQueryResult:
        if not entry.title:
            return SourceQueryResult(source=self.name, query_kind="metadata", records=[])
        words = title_search_words(entry.title)
        if not words:
            # Nothing the word index can match. An empty result here would read as "DBLP has no
            # such paper" when DBLP was never actually asked.
            return SourceQueryResult(
                source=self.name, query_kind="metadata",
                error=f"title has no searchable word for DBLP: {entry.title!r}",
            )
        try:
            _status, found = await get_json(
                self.client, self.rate_limiter, _ENDPOINT,
                params={"query": build_search_query(words, limit)}, headers=_ACCEPT,
            )
            hits = _bindings(found)
            iris = list(dict.fromkeys(r.get("publ", {}).get("value", "") for r in hits))
            if not iris:
                return SourceQueryResult(source=self.name, query_kind="metadata", records=[])
            bad = [iri for iri in iris if not _PUBL_IRI_RE.match(iri)]
            if bad:
                raise _UnexpectedResponse(f"unexpected publication IRI(s) from DBLP: {bad[:3]}")
            _status, details = await get_json(
                self.client, self.rate_limiter, _ENDPOINT,
                params={"query": build_details_query(iris)}, headers=_ACCEPT,
            )
            rows = hits + _bindings(details)
        except (TransientHTTPError, _UnexpectedResponse) as exc:
            return SourceQueryResult(source=self.name, query_kind="metadata", error=str(exc))
        records: list[SourceRecord] = dblp_sparql_to_records(rows)
        return SourceQueryResult(source=self.name, query_kind="metadata", records=records)
