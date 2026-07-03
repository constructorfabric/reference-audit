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
    normalize_openalex_id,
)

# Crossref relation keys that point at another version of the same work.
_CROSSREF_VERSION_RELATIONS = (
    "is-preprint-of",
    "has-preprint",
    "is-version-of",
    "has-version",
    "is-identical-to",
)


def _normalize_isbn13s(raw: object) -> tuple[str, ...]:
    """Every distinct ISBN-13 in a source's ISBN field (print + electronic, etc.), order-preserving.

    Order is kept so the first becomes the record's canonical `isbn13`; the whole set drives matching
    and pooling so different printings of one book read as the same work, not a conflict."""
    if not raw:
        return ()
    items = [raw] if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for i in items:
        norm = normalize_isbn13(i if isinstance(i, str) else None)
        if norm and norm not in out:
            out.append(norm)
    return tuple(out)


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
    isbn13s = _normalize_isbn13s(item.get("ISBN"))
    isbn13 = isbn13s[0] if isbn13s else None
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
        ids=Identifiers(doi=doi, isbn13=isbn13, isbn13s=isbn13s, arxiv_id=arxiv),
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


def _openalex_abstract(work: dict) -> str:
    """Reconstruct the abstract from OpenAlex's `abstract_inverted_index` ({word: [positions]}).

    OpenAlex ships abstracts only in inverted form (a licensing constraint). Missing/empty index →
    empty string (never a guess). Positions can be sparse; we place each word and join in order.
    """
    index = work.get("abstract_inverted_index")
    if not isinstance(index, dict) or not index:
        return ""
    slots: list[tuple[int, str]] = []
    for word, positions in index.items():
        if isinstance(positions, list):
            for p in positions:
                if isinstance(p, int):
                    slots.append((p, word))
    slots.sort()
    return " ".join(word for _, word in slots).strip()


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
        abstract=_openalex_abstract(work),
        ids=Identifiers(doi=doi, arxiv_id=arxiv, pmid=pmid, openalex=normalize_openalex_id(work_id)),
        is_preprint=(work.get("type") == "preprint" or source_block.get("type") == "repository"),
        citation_count=int(work.get("cited_by_count") or 0),
        version_links=version_links,
        openalex_work_id=work_id,
        raw=work,
    )


# ── DBLP (computer-science venues — the premier ML conferences) ────────────────
# DBLP appends a 4-digit homonym-disambiguation number to non-unique names ("Bowen Baker 0001");
# it is not part of the name and must be stripped before author comparison.
_DBLP_HOMONYM_RE = re.compile(r"\s+\d{4}$")


def _dblp_authors(info: dict) -> list[str]:
    """DBLP collapses a single-element array to a bare object, so `authors.author` may be a list of
    `{text, @pid}` dicts, one such dict, or absent. Normalize all shapes to a list of names."""
    block = info.get("authors") or {}
    raw = block.get("author")
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    for a in items:
        name = (a.get("text") if isinstance(a, dict) else str(a)) or ""
        name = _DBLP_HOMONYM_RE.sub("", name.strip())
        if name:
            out.append(name)
    return out


def _dblp_venue(info: dict) -> str:
    """`venue` is a string, or a list when the record spans several streams — take the first."""
    venue = info.get("venue")
    if isinstance(venue, list):
        return (venue[0] if venue else "").strip()
    return (venue or "").strip()


def dblp_hit_to_record(hit: dict) -> SourceRecord:
    """One DBLP publ-search `hit` → SourceRecord.

    DBLP authoritatively indexes the premier CS/ML venues (NeurIPS, ICLR, ICML/PMLR), which mint no
    DOI and are thinly/ambiguously covered by the article-centric aggregators. The `ee` field is the
    electronic-edition landing page — typically the very proceedings URL the `.bib` cites — so it is
    kept as `ids.url`; a DOI is captured when present, and an arXiv id is recovered from an `ee`
    pointing at arxiv.org (DBLP's "Informal and Other Publications" preprint records).
    """
    info = hit.get("info") or {}
    title = (info.get("title") or "").strip().rstrip(".")
    ee = (info.get("ee") or "").strip()
    doi = normalize_doi(info.get("doi")) or normalize_doi(ee)
    arxiv = extract_arxiv_id(None, None, fallback_text=ee)
    year = info.get("year")
    rec_type = (info.get("type") or "")
    return SourceRecord(
        source="dblp",
        source_native_id=(info.get("key") or "").strip(),
        title=title,
        authors=_dblp_authors(info),
        year=int(year) if year and str(year).isdigit() else None,
        venue=_dblp_venue(info),
        pages=(info.get("pages") or "").strip(),
        ids=Identifiers(doi=doi, arxiv_id=arxiv, url=ee or None),
        is_preprint="informal" in rec_type.lower(),
        raw=hit,
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
        abstract=(paper.get("abstract") or "").strip(),
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
    isbn13s = _normalize_isbn13s(doc.get("isbn"))
    isbn13 = isbn13s[0] if isbn13s else None
    authors = doc.get("author_name") or []
    publishers = doc.get("publisher") or []
    return SourceRecord(
        source="openlibrary",
        source_native_id=(doc.get("key") or "").strip(),
        title=(doc.get("title") or "").strip(),
        authors=[a.strip() for a in authors if a],
        year=doc.get("first_publish_year"),
        publisher=(publishers[0].strip() if publishers else ""),
        ids=Identifiers(isbn13=isbn13, isbn13s=isbn13s),
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
    isbn13s = _normalize_isbn13s((edition.get("isbn_13") or []) + (edition.get("isbn_10") or []))
    isbn13 = isbn13s[0] if isbn13s else None
    publishers = edition.get("publishers") or []
    return SourceRecord(
        source="openlibrary",
        source_native_id=(edition.get("key") or "").strip(),
        title=(edition.get("title") or work_title or "").strip(),
        year=_openlibrary_edition_year(edition.get("publish_date")),
        publisher=(publishers[0].strip() if publishers else ""),
        ids=Identifiers(isbn13=isbn13, isbn13s=isbn13s),
        raw=edition,
    )


# ── Google Books (books) ──────────────────────────────────────────────────────
# A 4-digit year anywhere in a Google Books `publishedDate` ("2012", "2012-03-20").
_GBOOKS_YEAR = re.compile(r"\b(1[5-9]\d\d|20\d\d)\b")


def _google_books_isbn13s(info: dict) -> tuple[str, ...]:
    """Every ISBN-13 in `industryIdentifiers` (ISBN_13s first, then ISBN_10s normalized to 13)."""
    idents = info.get("industryIdentifiers") or []
    raw13 = [i.get("identifier") for i in idents if (i.get("type") or "") == "ISBN_13"]
    raw10 = [i.get("identifier") for i in idents if (i.get("type") or "") == "ISBN_10"]
    return _normalize_isbn13s(raw13 + raw10)


def google_books_volume_to_record(volume: dict) -> SourceRecord:
    """One Google Books `volumes` item (volume or search hit) → SourceRecord.

    The cited `title` in a .bib usually includes the subtitle ("Why Nations Fail: The Origins…"),
    which Google Books stores split across `title` + `subtitle`; we recombine them so the matcher's
    title comparison sees the same string the entry carries.
    """
    info = volume.get("volumeInfo") or {}
    title = (info.get("title") or "").strip()
    subtitle = (info.get("subtitle") or "").strip()
    full_title = f"{title}: {subtitle}" if subtitle else title
    m = _GBOOKS_YEAR.search(info.get("publishedDate") or "")
    isbn13s = _google_books_isbn13s(info)
    return SourceRecord(
        source="google_books",
        source_native_id=(volume.get("id") or "").strip(),
        title=full_title,
        authors=[a.strip() for a in (info.get("authors") or []) if a],
        year=int(m.group(1)) if m else None,
        publisher=(info.get("publisher") or "").strip(),
        ids=Identifiers(
            isbn13=isbn13s[0] if isbn13s else None,
            isbn13s=isbn13s,
            google_books=(volume.get("id") or "").strip() or None,
        ),
        raw=volume,
    )
