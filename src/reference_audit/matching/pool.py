"""Candidate pooling: merge source records that are the same work into one candidate.

Merging is IDENTIFIER-GROUNDED only — never title fuzz — so distinct works (distinct DOIs, T2) stay
separate, while:
  * records sharing an exact id (DOI / arXiv / ISBN13 / OpenAlex Work id) collapse, and
  * a preprint and its published version merge when one record's id appears in the other's
    `version_links` (the arXiv↔published edge — T1).
The richer record (DOI-bearing, then higher citation count) represents the group; ids and version
links are unioned, and both source records are retained on the merged candidate for the report.
The ambiguous, identifier-disjoint zone (similar titles, no link) is left to M5's LLM tie-break.
"""

from __future__ import annotations

from reference_audit.matching.features import title_prefix_trap, title_ratio
from reference_audit.matching.names import author_overlap, author_subset
from reference_audit.models import Identifiers, SourceRecord
from reference_audit.parsing.identifiers import extract_arxiv_id, normalize_doi

# Thresholds for the preprint↔published version merge (tight: titles are "usually correct").
_VERSION_MERGE_TITLE = 0.93
_VERSION_MERGE_AUTHOR = 0.8


def _own_keys(rec: SourceRecord) -> set[str]:
    keys: set[str] = set()
    if rec.ids.doi:
        keys.add(f"doi:{rec.ids.doi}")
    if rec.ids.arxiv_id:
        keys.add(f"arxiv:{rec.ids.arxiv_id.lower()}")
    # Pool on the canonical ISBN only — deliberately NOT the whole `all_isbn13()` set. A book
    # *chapter*'s scholarly record inherits its containing volume's ISBNs, so pooling on any shared
    # ISBN would fuse sibling chapters (and the chapter with its parent book) into one record whose
    # representative is picked by citation-richness, not title — which could bury the title-matching
    # chapter. Same-work editions (print vs electronic) are still unified downstream by the
    # SAME-OBJECT clustering at verdict time, which compares title+author, not ISBN membership alone.
    if rec.ids.isbn13:
        keys.add(f"isbn:{rec.ids.isbn13}")
    if rec.openalex_work_id:
        keys.add(f"oa:{rec.openalex_work_id}")
    return keys


def _link_to_keys(link: str) -> set[str]:
    """Identifier keys referenced by a version-link string (URL or bare id)."""
    keys: set[str] = set()
    arxiv = extract_arxiv_id(None, None, fallback_text=link)
    if arxiv:
        keys.add(f"arxiv:{arxiv.lower()}")
    doi = normalize_doi(link)
    if doi and not doi.startswith("10.48550/arxiv"):  # the arXiv DOI is already captured as arxiv:
        keys.add(f"doi:{doi}")
    return keys


def _link_keys(rec: SourceRecord) -> set[str]:
    keys: set[str] = set()
    for link in rec.version_links:
        keys |= _link_to_keys(link)
    return keys


def _merge_ids(a: Identifiers, b: Identifiers) -> Identifiers:
    return Identifiers(
        doi=a.doi or b.doi,
        arxiv_id=a.arxiv_id or b.arxiv_id,
        isbn13=a.isbn13 or b.isbn13,
        isbn13s=tuple(dict.fromkeys((*a.isbn13s, *b.isbn13s, *a.all_isbn13(), *b.all_isbn13()))),
        pmid=a.pmid or b.pmid,
        bibcode=a.bibcode or b.bibcode,
        openalex=a.openalex or b.openalex,
        url=a.url or b.url,
    )


def _richness(rec: SourceRecord) -> tuple[int, int, int]:
    return (1 if rec.ids.doi else 0, rec.citation_count, len(rec.authors))


def _underlying_sources(rec: SourceRecord) -> list[str]:
    """Sources actually behind a record: the merge set if it is a pooled representative, else its own
    source. Pooling must be idempotent — re-pooling an already-merged record (the enrichment pass)
    must not lose provenance. (The prior two-phase pooling rebuilt representatives from earlier
    representatives and ranked them by a single lossy `.source` label, so a preprint copy could
    outrank a richer published record hidden inside another representative — the Flow-Lenia bug.)"""
    merged_from = rec.raw.get("merged_from") if isinstance(rec.raw, dict) else None
    return list(merged_from) if merged_from else [rec.source]


# Bibliographic fields are filled on the merged representative from the most authoritative source
# that carries them, independent of which record is the citation-richest. The publisher of record
# (the DOI landing-page citation export) outranks the aggregators; Crossref/OpenAlex (the
# registration-grade sources) outrank Semantic Scholar, whose venue strings are often truncated
# ('Complexity' → 'Complex') and which omits volume/issue/pages. Records are ranked by their *best
# underlying* source, so a pooled representative is ranked by the richest source inside it — not by
# its lossy `.source` label. This realizes the SPEC's "compile all available information".
_FIELD_SOURCE_PRIORITY: dict[str, tuple[str, ...]] = {
    "venue": ("publisher", "crossref", "openalex", "openlibrary"),
    "volume": ("publisher", "crossref", "openalex"),
    "issue": ("publisher", "crossref", "openalex"),
    "pages": ("publisher", "crossref", "openalex"),
    "publisher": ("publisher", "crossref", "openlibrary", "openalex"),
}


_YEAR_SOURCE_PRIORITY = ("publisher", "crossref", "openalex")


def _rank(rec: SourceRecord, priority: tuple[str, ...]) -> int:
    """Priority rank of a record by its best (lowest-rank) underlying source."""
    return min(
        (priority.index(s) if s in priority else len(priority))
        for s in _underlying_sources(rec)
    )


def _best_field(recs: list[SourceRecord], attr: str, priority: tuple[str, ...]) -> str:
    """Most authoritative non-empty value for `attr` across a same-work group ('' if none has it)."""
    for r in sorted(recs, key=lambda r: _rank(r, priority)):
        value = (getattr(r, attr) or "").strip()
        if value:
            return value
    return ""


def _best_abstract(recs: list[SourceRecord]) -> str:
    """The fullest abstract across a same-work group (longest non-empty).

    Abstracts come from OpenAlex / Semantic Scholar; the citation-richest representative may be a
    source (e.g. Crossref) that carries none, so — like the other bibliographic fields — the abstract
    is compiled across the group rather than taken only from the representative, so it survives onto
    the matched artifact (and the cached verdict) for the citation-alignment check.
    """
    return max(((r.abstract or "").strip() for r in recs), key=len, default="")


def _best_year(recs: list[SourceRecord]) -> int | None:
    """Publication year from the registration-grade source (publisher/Crossref/OpenAlex) when
    available.

    Semantic Scholar often reports the online/preprint year (it gave 2018 for the 2019 Complex
    Systems 'Lenia' DOI whose Crossref record is 2019), so its richer-citation record must not
    override the DOI registrant's year.
    """
    for r in sorted(recs, key=lambda r: _rank(r, _YEAR_SOURCE_PRIORITY)):
        if r.year:
            return r.year
    return None


def pool_candidates(records: list[SourceRecord]) -> list[SourceRecord]:
    """Merge records that describe the same work into one representative candidate each.

    A single union-find over all records with two kinds of (transitively closed) edge:
      * identifier edge — both records carry ids and they overlap, or one side's version link
        bridges the other's id (shared DOI/arXiv/ISBN13/OpenAlex id; the arXiv↔published edge).
        Grounded in identifiers, never title fuzz.
      * version edge — `_same_work_version`: a preprint and its published version, or two editions of
        one book, agreeing on title+authors. This is the *only* place title similarity is allowed,
        and only to UNITE versions of one work — two distinct published DOIs are still held apart by
        the V1 veto inside `_same_work_version`.

    Transitive closure (order-independent union-find) is essential and is why this replaced the
    earlier greedy first-match grouping: a Semantic Scholar record that carries BOTH the arXiv id and
    the published DOI must fuse the preprint group and the published group into one candidate. When
    they stayed split, canonical fields were sourced from the preprint copy (the Flow-Lenia bug).
    """
    n = len(records)
    if n <= 1:
        return list(records)

    own = [_own_keys(r) for r in records]
    links = [_link_keys(r) for r in records]
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(j)] = find(i)

    for i in range(n):
        for j in range(i + 1, n):
            if find(i) == find(j):
                continue
            id_edge = bool(
                own[i]
                and own[j]
                and ((own[i] & own[j]) or (own[i] & links[j]) or (links[i] & own[j]))
            )
            if id_edge or _same_work_version(records[i], records[j]):
                union(i, j)

    clusters: dict[int, list[SourceRecord]] = {}
    for idx in range(n):
        clusters.setdefault(find(idx), []).append(records[idx])
    return [_representative(g) if len(g) > 1 else g[0] for g in clusters.values()]


def _representative(recs: list[SourceRecord]) -> SourceRecord:
    best = max(recs, key=_richness)
    merged = best.model_copy(deep=True)
    for r in recs:
        merged.ids = _merge_ids(merged.ids, r.ids)
        for link in r.version_links:
            if link not in merged.version_links:
                merged.version_links.append(link)
        if r.openalex_work_id and not merged.openalex_work_id:
            merged.openalex_work_id = r.openalex_work_id
        merged.is_preprint = merged.is_preprint and r.is_preprint
    # Compile the best bibliographic metadata across the group (not just the richest record's).
    for attr, priority in _FIELD_SOURCE_PRIORITY.items():
        value = _best_field(recs, attr, priority)
        if value:
            setattr(merged, attr, value)
    best_year = _best_year(recs)
    if best_year is not None:
        merged.year = best_year
    best_abstract = _best_abstract(recs)
    if best_abstract:
        merged.abstract = best_abstract
    merged.raw = {"merged_from": sorted({s for r in recs for s in _underlying_sources(r)})}
    return merged


def _is_preprintish(rec: SourceRecord) -> bool:
    return rec.is_preprint or bool(rec.ids.doi and rec.ids.doi.startswith("10.48550/arxiv"))


def _published_doi(rec: SourceRecord) -> str | None:
    doi = rec.ids.doi
    return doi if doi and not doi.startswith("10.48550/arxiv") else None


def _title_authors_agree(a: SourceRecord, b: SourceRecord) -> bool:
    if title_ratio(a.title, b.title) < _VERSION_MERGE_TITLE:
        return False
    if title_prefix_trap(a.title, b.title, tail_threshold=0.34):
        return False  # V3: shared prefix, divergent tail (bagrov vs kravchenko)
    return author_overlap(a.authors, b.authors) >= _VERSION_MERGE_AUTHOR or author_subset(
        a.authors, b.authors
    )


def _same_work_version(a: SourceRecord, b: SourceRecord) -> bool:
    """Two records describe one work when titles+authors agree AND either:

    R1 (preprint↔published): exactly one side is a preprint — a version relationship that overrides
       the two-distinct-DOIs signal (the published DOI and the arXiv preprint DOI legitimately
       differ); or
    R2 (same edition/printing): the two do NOT both carry distinct *published* DOIs (one or both
       lack one) — different editions/reprints of one book, or a metadata-only duplicate.

    Two distinct *published* DOIs with neither side a preprint is left UNMERGED (the design's V1
    veto): it is a genuine "same surface" case for M5's LLM tie-break (e.g. duplicate DOIs for one
    paper), not a formal merge.
    """
    if not _title_authors_agree(a, b):
        return False
    if _is_preprintish(a) != _is_preprintish(b):  # R1
        return True
    pa, pb = _published_doi(a), _published_doi(b)
    return not (pa and pb and pa != pb)  # R2: merge unless two distinct published DOIs
