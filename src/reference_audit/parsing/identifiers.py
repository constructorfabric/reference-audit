"""Identifier extraction & normalization.

DOI/ISBN regexes lifted from `paper-search-mcp/paper_search_mcp/utils.py`; we add
URL-prefix stripping (fixes wolpert2007's `https://doi.org/...` DOI), ISBN-10→13 conversion,
and arXiv-id extraction from `eprint`/`archivePrefix`.
"""

from __future__ import annotations

import re

# --- DOI ---
_DOI_CORE_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


# @cpt-dod:cpt-referenceaudit-dod-parsing-identifiers:p1
def normalize_doi(text: str | None) -> str | None:
    """Return a bare, lowercased DOI ('10.xxxx/...') from any URL/prefixed form, or None.

    DOIs are case-insensitive (ISO 26324); lowercasing makes opaque-token equality trivial.
    """
    if not text:
        return None
    m = _DOI_CORE_RE.search(text)
    if not m:
        return None
    doi = m.group(0).rstrip(".,;)")
    return doi.lower()


# --- ISBN ---
def _compact_isbn(text: str) -> str:
    return re.sub(r"[\s\-]", "", str(text or "")).upper()


def _isbn10_to_13(isbn10: str) -> str | None:
    core = "978" + isbn10[:9]
    total = sum((1 if i % 2 == 0 else 3) * int(d) for i, d in enumerate(core))
    check = (10 - (total % 10)) % 10
    return core + str(check)


def normalize_isbn13(text: str | None) -> str | None:
    """Return a 13-digit ISBN (converting ISBN-10), or None if not a valid ISBN shape."""
    if not text:
        return None
    compact = _compact_isbn(text)
    if re.fullmatch(r"97[89]\d{10}", compact):
        return compact
    if re.fullmatch(r"\d{9}[\dX]", compact):
        return _isbn10_to_13(compact)
    # try to find an embedded ISBN
    for pat in (r"97[89](?:[\s\-]?\d){10}", r"(?:\d[\s\-]?){9}[\dXx]"):
        m = re.search(pat, text)
        if m:
            return normalize_isbn13(m.group(0))
    return None


# --- arXiv ---
# Bare ids are only trusted from an `eprint` field or after an explicit arXiv marker —
# never scraped from an arbitrary DOI (e.g. 10.3389/frobt.2016.00040 is NOT arXiv 2016.00040).
_ARXIV_NEW_RE = re.compile(r"\b(\d{4}\.\d{4,5})(?:v\d+)?\b")
_ARXIV_OLD_RE = re.compile(r"\b([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?\b", re.IGNORECASE)
_ARXIV_MARKED_RE = re.compile(
    r"(?:arxiv[._:/]|abs/)\s*/?([a-z\-]*(?:\.[A-Z]{2})?/?\d{4}\.\d{4,5}(?:v\d+)?)",
    re.IGNORECASE,
)


def _bare_arxiv(text: str) -> str | None:
    m = _ARXIV_NEW_RE.search(text) or _ARXIV_OLD_RE.search(text)
    return _strip_arxiv_version(m.group(1)) if m else None


def extract_arxiv_id(
    eprint: str | None, archive_prefix: str | None = None, *, fallback_text: str | None = None
) -> str | None:
    """Pull an arXiv id from a trusted `eprint` field (guarded by archivePrefix=arXiv when
    present), or from text that explicitly mentions arXiv (a 10.48550/arXiv.<id> DOI or an
    arxiv.org/abs/<id> URL). Arbitrary DOIs are never scanned for bare ids."""
    if eprint:
        prefix_ok = (not archive_prefix) or archive_prefix.strip().lower() == "arxiv"
        if prefix_ok:
            got = _bare_arxiv(eprint)
            if got:
                return got
    if fallback_text and re.search(r"arxiv|48550", fallback_text, re.IGNORECASE):
        m = _ARXIV_MARKED_RE.search(fallback_text)
        if m:
            return _strip_arxiv_version(m.group(1).lstrip("/"))
        got = _bare_arxiv(fallback_text)  # safe: an arXiv marker is already present
        if got:
            return got
    return None


def _strip_arxiv_version(arxiv_id: str) -> str:
    return re.sub(r"v\d+$", "", arxiv_id.strip())


def arxiv_to_doi(arxiv_id: str) -> str:
    """The DataCite DOI arXiv mints for every submission."""
    return f"10.48550/arxiv.{arxiv_id.lower()}"


def arxiv_submission_year(arxiv_id: str | None) -> int | None:
    """Year an arXiv id was first submitted — encoded in the id itself, so it pins the *original*
    version (v1) independent of any later updates.

    New scheme `YYMM.NNNNN` (since 2007-04) → 2000+YY. Old scheme `archive/YYMMNNN` (1991-2007) →
    19YY for YY>=91 else 20YY. Returns None when the id matches neither shape.
    """
    if not arxiv_id:
        return None
    s = _strip_arxiv_version(arxiv_id.strip())
    new = re.match(r"^(\d{2})\d{2}\.\d{4,5}$", s)
    if new:
        return 2000 + int(new.group(1))
    old = re.match(r"^[a-z-]+(?:\.[a-z]{2})?/(\d{2})\d{5}$", s, re.IGNORECASE)
    if old:
        yy = int(old.group(1))
        return 1900 + yy if yy >= 91 else 2000 + yy
    return None


def normalize_url(text: str | None) -> str | None:
    if not text:
        return None
    url = text.strip()
    return url or None


# --- OpenAlex Work id ---
# A canonical OpenAlex Work id is the token `W<digits>` carried by an openalex.org URL
# (https://openalex.org/W3034344071) or an API path (api.openalex.org/works/W...). Only trusted
# when an openalex.org host is present — a bare `W…` token is too ambiguous to scrape blindly.
_OPENALEX_ID_RE = re.compile(r"\bW\d+\b", re.IGNORECASE)


def normalize_openalex_id(text: str | None) -> str | None:
    """Return a bare, upper-cased OpenAlex Work id ('W3034344071') from an openalex.org URL, or None."""
    if not text or "openalex.org" not in text.lower():
        return None
    m = _OPENALEX_ID_RE.search(text)
    return m.group(0).upper() if m else None


# --- Google Books volume id ---
# A Google Books volume id is the opaque `id=` token on a books.google.<tld> (or
# play.google.com/books) URL — e.g. https://books.google.com.sg/books?id=yIV_NMDDIvYC. It is the
# authoritative key for that exact volume, so the adapter can resolve it with no fuzzy matching.
# Only trusted when a Google Books host is present — a bare token is too ambiguous to scrape.
_GBOOKS_ID_RE = re.compile(r"[?&]id=([A-Za-z0-9_-]+)")


def normalize_google_books_id(text: str | None) -> str | None:
    """Return a Google Books volume id from a books.google / play.google books URL, or None."""
    if not text:
        return None
    low = text.lower()
    if "books.google." not in low and "play.google.com/books" not in low:
        return None
    m = _GBOOKS_ID_RE.search(text)
    return m.group(1) if m else None
