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
        pmid=a.pmid or b.pmid,
        bibcode=a.bibcode or b.bibcode,
        url=a.url or b.url,
    )


def _richness(rec: SourceRecord) -> tuple[int, int, int]:
    return (1 if rec.ids.doi else 0, rec.citation_count, len(rec.authors))


# Bibliographic fields are filled on the merged representative from the most authoritative source
# that carries them, independent of which record is the citation-richest. Crossref/OpenAlex (the
# registration-grade sources) outrank Semantic Scholar, whose venue strings are often truncated
# ('Complexity' → 'Complex') and which omits volume/issue/pages. This realizes the SPEC's "compile
# all available information" so downstream field checks compare against complete, correct metadata.
_FIELD_SOURCE_PRIORITY: dict[str, tuple[str, ...]] = {
    "venue": ("crossref", "openalex", "openlibrary"),
    "volume": ("crossref", "openalex"),
    "issue": ("crossref", "openalex"),
    "pages": ("crossref", "openalex"),
    "publisher": ("crossref", "openlibrary", "openalex"),
}


_YEAR_SOURCE_PRIORITY = ("crossref", "openalex")


def _rank(source: str, priority: tuple[str, ...]) -> int:
    return priority.index(source) if source in priority else len(priority)


def _best_field(recs: list[SourceRecord], attr: str, priority: tuple[str, ...]) -> str:
    """Most authoritative non-empty value for `attr` across a same-work group ('' if none has it)."""
    for r in sorted(recs, key=lambda r: _rank(r.source, priority)):
        value = (getattr(r, attr) or "").strip()
        if value:
            return value
    return ""


def _best_year(recs: list[SourceRecord]) -> int | None:
    """Publication year from the registration-grade source (Crossref/OpenAlex) when available.

    Semantic Scholar often reports the online/preprint year (it gave 2018 for the 2019 Complex
    Systems 'Lenia' DOI whose Crossref record is 2019), so its richer-citation record must not
    override the DOI registrant's year.
    """
    for r in sorted(recs, key=lambda r: _rank(r.source, _YEAR_SOURCE_PRIORITY)):
        if r.year:
            return r.year
    return None


def pool_candidates(records: list[SourceRecord]) -> list[SourceRecord]:
    """Group records into same-work candidates; return one representative per group."""
    groups: list[dict] = []  # {"own": set, "links": set, "records": list}

    for rec in records:
        own = _own_keys(rec)
        links = _link_keys(rec)
        if not own:
            # No identifier: cannot be safely merged here — keep separate (M5 may merge via LLM).
            groups.append({"own": set(), "links": links, "records": [rec]})
            continue
        # Merge into a group if our ids overlap its ids, OR either side's version links bridge them.
        hit = next(
            (
                g
                for g in groups
                if (own & g["own"]) or (own & g["links"]) or (links & g["own"])
            ),
            None,
        )
        if hit is None:
            groups.append({"own": set(own), "links": set(links), "records": [rec]})
        else:
            hit["own"] |= own
            hit["links"] |= links
            hit["records"].append(rec)

    representatives = [_representative(g["records"]) for g in groups]
    return _merge_preprint_published(representatives)


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
    merged.raw = {"merged_from": sorted({r.source for r in recs})}
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


def _merge_preprint_published(reps: list[SourceRecord]) -> list[SourceRecord]:
    """Second pass: union representatives that are preprint↔published versions of one work."""
    parent = list(range(len(reps)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(reps)):
        for j in range(i + 1, len(reps)):
            if find(i) != find(j) and _same_work_version(reps[i], reps[j]):
                parent[find(j)] = find(i)

    clusters: dict[int, list[SourceRecord]] = {}
    for idx, rec in enumerate(reps):
        clusters.setdefault(find(idx), []).append(rec)
    return [_representative(group) if len(group) > 1 else group[0] for group in clusters.values()]
