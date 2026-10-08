r"""BibTeX parsing → BibEntry.

Uses bibtexparser v1 (`loads` + `db.entries`, `convert_to_unicode`). Three non-standard behaviors:

1. **Commented-twin detection.** The pilot has a `%@misc{bagrov2024visual, ...}` block whose
   only-commented header is the arXiv preprint of `kravchenko2026`. Whether bibtexparser drops
   it or (because `%` is not a BibTeX comment char) parses it anyway, we decide commentedness
   from the *raw source line* and route such entries to `twins` (informational), never the
   audited list — T1 is solved from the DBs, not from this block.
2. **LaTeX-accent decode** via `convert_to_unicode` ({\'e}→é) for clean matching.
3. **Dropped-entry detection.** bibtexparser 1.x skips an entry it cannot parse (an unbalanced brace,
   most often) without a word. Every live `@type{key` header in the raw text that produced no entry
   is returned as an `UnparsedEntry` with the reason, so the report can say it was not checked.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import bibtexparser
from bibtexparser.bparser import BibTexParser
from bibtexparser.customization import convert_to_unicode

from reference_audit.models import BibEntry, Identifiers, UnparsedEntry, entry_type_from_bib
from reference_audit.parsing.identifiers import (
    extract_arxiv_id,
    normalize_doi,
    normalize_google_books_id,
    normalize_isbn13,
    normalize_openalex_id,
    normalize_url,
)

# An @type{key occurrence; we inspect whether a '%' precedes the '@' on the same line.
_ENTRY_LINE_RE = re.compile(r"(?m)^(?P<pre>[^\n@]*)@(?P<type>\w+)\s*\{\s*(?P<key>[^,\s}]+)")
_FIELD_RE = re.compile(r"(\w+)\s*=\s*[{\"]([^{}\"]*)[}\"]")
# `@string`, `@preamble` and `@comment` blocks are not entries.
_NON_ENTRY_TYPES = {"string", "preamble", "comment"}


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
    # A books.google `url` carries a resolvable volume id — extract it as a first-class identifier
    # (the authoritative key for that exact volume). The URL itself is kept (it is a real landing
    # page, unlike an openalex.org Work URL) so a dead-link check can still see it.
    google_books = normalize_google_books_id(f.get("url"))
    url = None if openalex else normalize_url(f.get("url"))
    pmid = (f.get("pmid") or "").strip() or None
    return Identifiers(
        doi=doi, arxiv_id=arxiv, isbn13=isbn13, url=url, pmid=pmid,
        openalex=openalex, google_books=google_books,
    )


def entry_from_fields(
    key: str,
    bib_type: str,
    f: dict[str, str],
    *,
    commented: bool = False,
    authors: list[str] | None = None,
) -> BibEntry:
    """Build a `BibEntry` from a flat `{field: str}` dict — the single construction seam.

    Any front end that produces BibTeX-shaped fields goes through here (the `.bib` parser below, and
    `pdf.tei` for GROBID's TEI), so identifier normalization, venue selection, cleaning and type
    mapping are defined exactly once.

    `authors` bypasses the `" and "` split for callers that already have a *list* of names. TEI gives
    us structured `<persName>` elements, and re-joining them into `"A and B"` only to re-split would
    corrupt an organizational author such as "Smith and Sons".
    """
    venue = f.get("journal") or f.get("booktitle") or f.get("howpublished") or ""
    return BibEntry(
        key=key,
        entry_type=entry_type_from_bib(bib_type),
        title=_clean(f.get("title", "")),
        authors=authors if authors is not None else _split_authors(f.get("author", "")),
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
    return entry_from_fields(key, bib_type, fields, commented=True)


class BibParse(NamedTuple):
    entries: list[BibEntry]          # audited
    twins: list[BibEntry]            # commented-out entries (informational)
    unparsed: list[UnparsedEntry]    # live entries bibtexparser could not read (never audited)


def _brace_balance(text: str) -> int:
    """Net `{` minus `}` in `text`, ignoring escaped braces (`\\{`, `\\}`)."""
    unescaped = re.sub(r"\\[{}]", "", text)
    return unescaped.count("{") - unescaped.count("}")


def _unparsed_entries(
    raw: str, parsed_keys: set[str], commented_only: set[str]
) -> list[UnparsedEntry]:
    """Live entry headers in `raw` that produced no parsed entry, each with the likeliest reason.

    The block of an entry runs from its header to the next header. Its net brace count is what
    bibtexparser trips over: a block that does not balance is reported as such; otherwise the reason is
    the generic one, never a guess.
    """
    headers = [
        m for m in _ENTRY_LINE_RE.finditer(raw)
        if m.group("type").lower() not in _NON_ENTRY_TYPES
        and not re.search(r"(?<!\\)%", m.group("pre"))
    ]
    out: list[UnparsedEntry] = []
    reported: set[str] = set()
    for i, m in enumerate(headers):
        key = m.group("key")
        if key in parsed_keys or key in commented_only or key in reported:
            continue
        end = headers[i + 1].start() if i + 1 < len(headers) else len(raw)
        balance = _brace_balance(raw[m.start("type") - 1:end])
        if balance > 0:
            reason = f"unbalanced braces ({balance} more '{{' than '}}')"
        elif balance < 0:
            reason = f"unbalanced braces ({-balance} more '}}' than '{{')"
        else:
            reason = "bibtexparser could not parse this entry"
        line = raw.count("\n", 0, m.start()) + 1
        out.append(UnparsedEntry(
            key=key, entry_type=m.group("type").lower(), line=line,
            reason=f"{reason}; the entry was not read, so it was not checked",
        ))
        reported.add(key)
    return out


def parse_bib(bib_path: str | Path) -> BibParse:
    """Parse a .bib file. Returns (audited_entries, commented_twins, unparsed_entries)."""
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
        entry = entry_from_fields(key, bib_type, fields, commented=commented)
        seen_keys.add(key)
        (twins if commented else entries).append(entry)

    # commented-only keys bibtexparser dropped entirely → reconstruct the twin from raw text
    for key in commented_only - seen_keys:
        twin = _twin_from_raw(raw, key)
        if twin is not None:
            twins.append(twin)

    return BibParse(entries, twins, _unparsed_entries(raw, seen_keys, commented_only))
