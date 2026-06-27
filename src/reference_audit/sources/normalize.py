"""Raw source JSON → normalized SourceRecord.

The Crossref normalizer keeps the version graph (`relation` targets → `version_links`,
`type`/posted-content → `is_preprint`) so the matcher can recover preprint↔published edges (M2).
OpenAlex/S2 normalizers are added in M3 with `locations[]` → `version_links` + `openalex_work_id`.
"""

from __future__ import annotations

from reference_audit.models import Identifiers, SourceRecord
from reference_audit.parsing.identifiers import (
    extract_arxiv_id,
    normalize_doi,
    normalize_isbn13,
)

# Crossref relation keys that point at another version of the same work.
_CROSSREF_VERSION_RELATIONS = (
    "is-preprint-of",
    "has-preprint",
    "is-version-of",
    "has-version",
    "is-identical-to",
)


def _crossref_year(item: dict) -> int | None:
    for field in ("published", "issued", "published-online", "published-print", "created"):
        block = item.get(field) or {}
        parts = block.get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                continue
    return None


def _crossref_authors(item: dict) -> list[str]:
    authors: list[str] = []
    for a in item.get("author", []) or []:
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        name = (a.get("name") or "").strip()
        full = f"{given} {family}".strip() or name
        if full:
            authors.append(full)
    return authors


def _crossref_version_links(item: dict) -> list[str]:
    links: list[str] = []
    relation = item.get("relation") or {}
    for rel in _CROSSREF_VERSION_RELATIONS:
        for target in relation.get(rel, []) or []:
            tid = (target.get("id") or "").strip()
            if tid:
                links.append(tid)
    return links


def crossref_item_to_record(item: dict) -> SourceRecord:
    doi = normalize_doi(item.get("DOI"))
    title_list = item.get("title") or []
    title = (title_list[0] if title_list else "").strip()
    venue_list = item.get("container-title") or []
    venue = (venue_list[0] if venue_list else "").strip()
    isbn_list = item.get("ISBN") or []
    isbn13 = normalize_isbn13(isbn_list[0]) if isbn_list else None
    version_links = _crossref_version_links(item)
    arxiv = extract_arxiv_id(None, None, fallback_text=" ".join([doi or "", *version_links]))

    return SourceRecord(
        source="crossref",
        source_native_id=doi or "",
        title=title,
        authors=_crossref_authors(item),
        year=_crossref_year(item),
        venue=venue,
        pages=(item.get("page") or "").strip(),
        ids=Identifiers(doi=doi, isbn13=isbn13, arxiv_id=arxiv),
        is_preprint=(item.get("type") == "posted-content"),
        citation_count=int(item.get("is-referenced-by-count") or 0),
        version_links=version_links,
        raw=item,
    )


# ── OpenAlex ──────────────────────────────────────────────────────────────────
def _openalex_authors(work: dict) -> list[str]:
    out: list[str] = []
    for a in work.get("authorships", []) or []:
        name = ((a.get("author") or {}).get("display_name") or "").strip()
        if name:
            out.append(name)
    return out


def _openalex_version_links(work: dict) -> list[str]:
    """All landing-page URLs across locations — the preprint↔published version graph (T1)."""
    links: list[str] = []
    for loc in work.get("locations", []) or []:
        url = (loc.get("landing_page_url") or "").strip()
        if url:
            links.append(url)
        pdf = (loc.get("pdf_url") or "").strip()
        if pdf:
            links.append(pdf)
    return links


def openalex_work_to_record(work: dict) -> SourceRecord:
    ids_block = work.get("ids") or {}
    doi = normalize_doi(work.get("doi") or ids_block.get("doi"))
    work_id = (work.get("id") or ids_block.get("openalex") or "").strip() or None
    title = (work.get("title") or work.get("display_name") or "").strip()
    primary = work.get("primary_location") or {}
    source_block = primary.get("source") or {}
    venue = (source_block.get("display_name") or "").strip()
    version_links = _openalex_version_links(work)
    pmid = ids_block.get("pmid")
    if pmid:
        pmid = pmid.rstrip("/").split("/")[-1] or None
    # arXiv id may live in a location URL even when the Work carries the published DOI (the merge)
    arxiv = extract_arxiv_id(None, None, fallback_text=" ".join([doi or "", *version_links]))
    return SourceRecord(
        source="openalex",
        source_native_id=work_id or doi or "",
        title=title,
        authors=_openalex_authors(work),
        year=work.get("publication_year"),
        venue=venue,
        ids=Identifiers(doi=doi, arxiv_id=arxiv, pmid=pmid),
        is_preprint=(work.get("type") == "preprint" or source_block.get("type") == "repository"),
        citation_count=int(work.get("cited_by_count") or 0),
        version_links=version_links,
        openalex_work_id=work_id,
        raw=work,
    )


# ── Semantic Scholar ──────────────────────────────────────────────────────────
def s2_paper_to_record(paper: dict) -> SourceRecord:
    ext = paper.get("externalIds") or {}
    doi = normalize_doi(ext.get("DOI"))
    arxiv = ext.get("ArXiv")
    arxiv = arxiv.strip() if arxiv else None
    pmid = ext.get("PubMed")
    authors = [a.get("name", "").strip() for a in (paper.get("authors") or []) if a.get("name")]
    return SourceRecord(
        source="semantic_scholar",
        source_native_id=(paper.get("paperId") or "").strip(),
        title=(paper.get("title") or "").strip(),
        authors=authors,
        year=paper.get("year"),
        venue=(paper.get("venue") or "").strip(),
        ids=Identifiers(doi=doi, arxiv_id=arxiv, pmid=str(pmid) if pmid else None),
        is_preprint=bool(arxiv and not doi),
        citation_count=int(paper.get("citationCount") or 0),
        raw=paper,
    )


# ── Open Library (books) ──────────────────────────────────────────────────────
def openlibrary_doc_to_record(doc: dict) -> SourceRecord:
    isbns = doc.get("isbn") or []
    isbn13 = next((normalize_isbn13(i) for i in isbns if normalize_isbn13(i)), None)
    authors = doc.get("author_name") or []
    return SourceRecord(
        source="openlibrary",
        source_native_id=(doc.get("key") or "").strip(),
        title=(doc.get("title") or "").strip(),
        authors=[a.strip() for a in authors if a],
        year=doc.get("first_publish_year"),
        ids=Identifiers(isbn13=isbn13),
        edition=doc.get("edition_count"),
        raw=doc,
    )
