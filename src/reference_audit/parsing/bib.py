r"""BibTeX parsing → BibEntry.

Uses bibtexparser v1 (`loads` + `db.entries`, `convert_to_unicode`). Two non-standard behaviors:

1. **Commented-twin detection.** The pilot has a `%@misc{bagrov2024visual, ...}` block whose
   only-commented header is the arXiv preprint of `kravchenko2026`. Whether bibtexparser drops
   it or (because `%` is not a BibTeX comment char) parses it anyway, we decide commentedness
   from the *raw source line* and route such entries to `twins` (informational), never the
   audited list — T1 is solved from the DBs, not from this block.
2. **LaTeX-accent decode** via `convert_to_unicode` ({\'e}→é) for clean matching.
"""

from __future__ import annotations

import re
from pathlib import Path

import bibtexparser
from bibtexparser.bparser import BibTexParser
from bibtexparser.customization import convert_to_unicode

from reference_audit.models import BibEntry, Identifiers, entry_type_from_bib
from reference_audit.parsing.identifiers import (
    extract_arxiv_id,
    normalize_doi,
    normalize_isbn13,
    normalize_openalex_id,
    normalize_url,
)

# An @type{key occurrence; we inspect whether a '%' precedes the '@' on the same line.
_ENTRY_LINE_RE = re.compile(r"(?m)^(?P<pre>[^\n@]*)@(?P<type>\w+)\s*\{\s*(?P<key>[^,\s}]+)")
_FIELD_RE = re.compile(r"(\w+)\s*=\s*[{\"]([^{}\"]*)[}\"]")


def _classify_keys(raw: str) -> set[str]:
    """Return keys whose @type{key header appears ONLY in a commented (`%`-prefixed) line."""
    commented: set[str] = set()
    live: set[str] = set()
    for m in _ENTRY_LINE_RE.finditer(raw):
        key = m.group("key")
        pre = m.group("pre")
        # A '%' anywhere before '@' on the line (not escaped) ⇒ commented occurrence.
        if re.search(r"(?<!\\)%", pre):
            commented.add(key)
        else:
            live.add(key)
    return commented - live


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("{", "").replace("}", "")).strip()


def _split_authors(author_field: str) -> list[str]:
    if not author_field:
        return []
    parts = re.split(r"\s+and\s+", author_field.strip())
    return [_clean(p) for p in parts if _clean(p)]


def _year(value: str | None) -> int | None:
    if not value:
        return None
    m = re.search(r"\d{4}", value)
    return int(m.group(0)) if m else None


def _identifiers_from_fields(f: dict[str, str]) -> Identifiers:
    doi = normalize_doi(f.get("doi")) or normalize_doi(f.get("url"))
    arxiv = extract_arxiv_id(
        f.get("eprint"),
        f.get("archiveprefix"),
        fallback_text=f.get("doi") or f.get("url"),
    )
    isbn13 = normalize_isbn13(f.get("isbn"))
    # An openalex.org `url` is a resolvable Work id, not a generic landing page: extract it as a
    # first-class identifier and drop it from `url` so it isn't mistaken for a web @misc page.
    openalex = normalize_openalex_id(f.get("url"))
    url = None if openalex else normalize_url(f.get("url"))
    pmid = (f.get("pmid") or "").strip() or None
    return Identifiers(
        doi=doi, arxiv_id=arxiv, isbn13=isbn13, url=url, pmid=pmid, openalex=openalex
    )


def _entry_from_fields(key: str, bib_type: str, f: dict[str, str], *, commented: bool) -> BibEntry:
    venue = f.get("journal") or f.get("booktitle") or f.get("howpublished") or ""
    return BibEntry(
        key=key,
        entry_type=entry_type_from_bib(bib_type),
        title=_clean(f.get("title", "")),
        authors=_split_authors(f.get("author", "")),
        year=_year(f.get("year")),
        venue=_clean(venue),
        publisher=_clean(f.get("publisher", "")),
        pages=_clean(f.get("pages", "")),
        ids=_identifiers_from_fields(f),
        raw_fields={k: v for k, v in f.items() if isinstance(v, str)},
        is_commented=commented,
    )


def _twin_from_raw(raw: str, key: str) -> BibEntry | None:
    """Best-effort parse of a commented-only block (no bibtexparser); used for the T1 twin."""
    m = re.search(r"(?m)^[^\n@]*@(\w+)\s*\{\s*" + re.escape(key) + r"\b", raw)
    if not m:
        return None
    bib_type = m.group(1)
    # capture from the header to the next line that is just a closing brace
    tail = raw[m.end():]
    end = re.search(r"(?m)^\s*\}\s*$", tail)
    block = tail[: end.start()] if end else tail
    fields = {k.lower(): v for k, v in _FIELD_RE.findall(block)}
    return _entry_from_fields(key, bib_type, fields, commented=True)


def parse_bib(bib_path: str | Path) -> tuple[list[BibEntry], list[BibEntry]]:
    """Parse a .bib file. Returns (audited_entries, commented_twins)."""
    raw = Path(bib_path).read_text(encoding="utf-8", errors="replace")
    commented_only = _classify_keys(raw)

    parser = BibTexParser(common_strings=True)
    parser.customization = convert_to_unicode
    parser.ignore_nonstandard_types = False
    db = bibtexparser.loads(raw, parser)

    entries: list[BibEntry] = []
    twins: list[BibEntry] = []
    seen_keys: set[str] = set()

    for rec in db.entries:
        key = rec.get("ID", "")
        bib_type = rec.get("ENTRYTYPE", "")
        fields = {k.lower(): v for k, v in rec.items() if k not in ("ID", "ENTRYTYPE")}
        commented = key in commented_only
        entry = _entry_from_fields(key, bib_type, fields, commented=commented)
        seen_keys.add(key)
        (twins if commented else entries).append(entry)

    # commented-only keys bibtexparser dropped entirely → reconstruct the twin from raw text
    for key in commented_only - seen_keys:
        twin = _twin_from_raw(raw, key)
        if twin is not None:
            twins.append(twin)

    return entries, twins
