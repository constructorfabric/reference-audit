"""Raw source JSON → normalized SourceRecord.

The Crossref normalizer keeps the version graph (`relation` targets → `version_links`,
`type`/posted-content → `is_preprint`) so the matcher can recover preprint↔published edges (M2).
OpenAlex/S2 normalizers are added in M3 with `locations[]` → `version_links` + `openalex_work_id`.
"""

from __future__ import annotations

import re

import bibtexparser
from bibtexparser.bparser import BibTexParser
from bibtexparser.customization import convert_to_unicode

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
        volume=(item.get("volume") or "").strip(),
        issue=(item.get("issue") or "").strip(),
        # Article-numbered venues (many proceedings/e-journals) register the sequence number under
        # `article-number` with no page range; fall back to it so it isn't reported unverifiable.
        pages=(item.get("page") or item.get("article-number") or "").strip(),
        publisher=(item.get("publisher") or "").strip(),
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


def _openalex_pages(biblio: dict) -> str:
    """OpenAlex stores first/last page separately; recombine into a '--' range."""
    first = (str(biblio.get("first_page") or "")).strip()
    last = (str(biblio.get("last_page") or "")).strip()
    if first and last:
        return first if first == last else f"{first}--{last}"
    return first or last


def openalex_work_to_record(work: dict) -> SourceRecord:
    ids_block = work.get("ids") or {}
    doi = normalize_doi(work.get("doi") or ids_block.get("doi"))
    work_id = (work.get("id") or ids_block.get("openalex") or "").strip() or None
    title = (work.get("title") or work.get("display_name") or "").strip()
    primary = work.get("primary_location") or {}
    source_block = primary.get("source") or {}
    venue = (source_block.get("display_name") or "").strip()
    biblio = work.get("biblio") or {}
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
        volume=(str(biblio.get("volume") or "")).strip(),
        issue=(str(biblio.get("issue") or "")).strip(),
        pages=_openalex_pages(biblio),
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


# ── Publisher of record (DOI landing-page citation export) ────────────────────
def _bib_clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("{", "").replace("}", "")).strip()


def _bib_authors(field: str | None) -> list[str]:
    if not field:
        return []
    return [_bib_clean(p) for p in re.split(r"\s+and\s+", field.strip()) if _bib_clean(p)]


def _bib_year(field: str | None) -> int | None:
    m = re.search(r"\d{4}", field or "")
    return int(m.group(0)) if m else None


# A `volume` that is really a proceedings *title* (Silverchair @proceedings exports put the volume
# title here) rather than a number — don't expose it as a numeric volume to compare against.
_NUMERICISH_VOLUME = re.compile(r"^[\w.\-/]{1,16}$")


def publisher_bibtex_to_record(bibtex: str, *, source: str = "publisher") -> SourceRecord | None:
    """Parse a publisher's citation-export BibTeX (the authority of record) into a SourceRecord.

    Its value is the registration-grade fields the aggregators lack — notably pages/article number
    for article-numbered venues (e.g. MIT Press/Silverchair proceedings, where Crossref's `page` is
    null but the export carries `pages={131}`). Returns None when the text is not BibTeX (e.g. a
    bot-challenge HTML page), so the caller reports a clean gap instead of a guess.
    """
    parser = BibTexParser(common_strings=True)
    parser.customization = convert_to_unicode
    parser.ignore_nonstandard_types = False
    try:
        db = bibtexparser.loads(bibtex, parser)
    except Exception:  # noqa: BLE001 — malformed/HTML payload is a clean "not retrievable"
        return None
    if not db.entries:
        return None
    rec = db.entries[0]
    f = {k.lower(): v for k, v in rec.items() if k not in ("ID", "ENTRYTYPE")}
    doi = normalize_doi(f.get("doi")) or normalize_doi(f.get("url"))
    volume = _bib_clean(f.get("volume"))
    if volume and not _NUMERICISH_VOLUME.match(volume):
        volume = ""  # a proceedings-title volume, not a number
    return SourceRecord(
        source=source,
        source_native_id=doi or "",
        title=_bib_clean(f.get("title")),
        authors=_bib_authors(f.get("author")),
        year=_bib_year(f.get("year")),
        venue=_bib_clean(f.get("journal") or f.get("booktitle")),
        volume=volume,
        issue=_bib_clean(f.get("number") or f.get("issue")),
        pages=_bib_clean(f.get("pages")),
        publisher=_bib_clean(f.get("publisher")),
        ids=Identifiers(doi=doi),
        is_preprint=False,
        raw={"merged_from": [source]},
    )


# ── Open Library (books) ──────────────────────────────────────────────────────
def openlibrary_doc_to_record(doc: dict) -> SourceRecord:
    isbns = doc.get("isbn") or []
    isbn13 = next((normalize_isbn13(i) for i in isbns if normalize_isbn13(i)), None)
    authors = doc.get("author_name") or []
    publishers = doc.get("publisher") or []
    return SourceRecord(
        source="openlibrary",
        source_native_id=(doc.get("key") or "").strip(),
        title=(doc.get("title") or "").strip(),
        authors=[a.strip() for a in authors if a],
        year=doc.get("first_publish_year"),
        publisher=(publishers[0].strip() if publishers else ""),
        ids=Identifiers(isbn13=isbn13),
        edition=doc.get("edition_count"),
        raw=doc,
    )


# A 4-digit year anywhere in an Open Library `publish_date` ("1976", "January 15, 2000", "c1976").
_OL_EDITION_YEAR = re.compile(r"\b(1[5-9]\d\d|20\d\d)\b")


def _openlibrary_edition_year(publish_date: str | None) -> int | None:
    m = _OL_EDITION_YEAR.search(publish_date or "")
    return int(m.group(1)) if m else None


def openlibrary_edition_to_record(edition: dict, *, work_title: str = "") -> SourceRecord:
    """One concrete Open Library *edition* (from a work's `editions.json`) → SourceRecord.

    Unlike `openlibrary_doc_to_record` (which collapses a whole work to its first-published year and
    an aggregate publisher list), this keeps each edition's own year/publisher/ISBN — exactly the
    per-edition granularity the book check needs to ground the *cited* edition and to point at the
    latest one.
    """
    isbns = (edition.get("isbn_13") or []) + (edition.get("isbn_10") or [])
    isbn13 = next((normalize_isbn13(i) for i in isbns if normalize_isbn13(i)), None)
    publishers = edition.get("publishers") or []
    return SourceRecord(
        source="openlibrary",
        source_native_id=(edition.get("key") or "").strip(),
        title=(edition.get("title") or work_title or "").strip(),
        year=_openlibrary_edition_year(edition.get("publish_date")),
        publisher=(publishers[0].strip() if publishers else ""),
        ids=Identifiers(isbn13=isbn13),
        raw=edition,
    )
