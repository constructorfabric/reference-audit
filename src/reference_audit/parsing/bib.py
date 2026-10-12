r"""BibTeX parsing → BibEntry.

bibtexparser 2 only splits the text into blocks (`parse_string` with an empty parse stack). This
module reads the field values itself, because the library's default middlewares would get them wrong
in ways nothing reports: they leave a `#` concatenation (`venue # " 2020"`) and a month macro
(`month = jul`) as raw text, and their LaTeX decoder truncates a URL at its first `%`
(`parsing.latex`). `read_bib_records` resolves `@string` references, `#` concatenation and BibTeX's
month macros, then decodes LaTeX (`decode_latex`: {\'e}→é) for clean matching. It never guesses: a
reference to an undefined `@string`, a duplicate field, or a value pylatexenc cannot read makes the
entry unparsed, with the reason. Three further non-standard behaviors:

1. **Commented-twin detection.** The pilot has a `%@misc{bagrov2024visual, ...}` block whose
   only-commented header is the arXiv preprint of `kravchenko2026`. bibtexparser reads a `%` line as
   a comment only up to the next `@`, so it may or may not parse such a block, and a fully commented
   one parses into fields named `%  title`. Commentedness is therefore decided from the *raw source
   line*, and every such entry is read from the uncommented text and routed to `twins`
   (informational), never the audited list. T1 is solved from the DBs, not from this block.
2. **Dropped-entry detection.** bibtexparser reports a block it cannot split as a failed block with
   no key. Every live `@type{key` header in the raw text that produced no entry is returned as an
   `UnparsedEntry` with the reason, so the report can say it was not checked.
3. **Duplicate keys.** bibtexparser sets a repeated entry key aside as a failed block. Both entries
   are audited, as written.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import bibtexparser
from bibtexparser.exceptions import BlockAbortedException
from bibtexparser.library import Library
from bibtexparser.model import (
    DuplicateBlockKeyBlock,
    DuplicateFieldKeyBlock,
    Entry,
    ParsingFailedBlock,
    String,
)

from reference_audit.models import BibEntry, Identifiers, UnparsedEntry, entry_type_from_bib
from reference_audit.parsing.entities import decode_html_entities
from reference_audit.parsing.identifiers import (
    extract_arxiv_id,
    normalize_doi,
    normalize_google_books_id,
    normalize_isbn13,
    normalize_openalex_id,
    normalize_url,
)
from reference_audit.parsing.latex import LatexDecodeError, decode_latex

# An @type{key occurrence; we inspect whether a '%' precedes the '@' on the same line.
_ENTRY_LINE_RE = re.compile(r"(?m)^(?P<pre>[^\n@]*)@(?P<type>\w+)\s*\{\s*(?P<key>[^,\s}]+)")
# `@string`, `@preamble` and `@comment` blocks are not entries.
_NON_ENTRY_TYPES = {"string", "preamble", "comment"}
# BibTeX's predefined month macros (`month = jul`), expanded to the full name as bibtex does.
_MONTH_MACROS = {
    name[:3].lower(): name
    for name in (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    )
}
# A bare operand of a field value: a `@string` name (or a number, checked first).
_STRING_NAME_RE = re.compile(r"[^\s\"#%'(),={}]+")
_NOT_CHECKED = "the entry was not read, so it was not checked"


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
    # HTML references (`d&apos;Amore`) come from web-scraped metadata; see `parsing.entities`.
    text = decode_html_entities(s.replace("{", "").replace("}", ""))
    return re.sub(r"\s+", " ", text).strip()


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


class _FieldError(ValueError):
    """A field value that cannot be read without guessing."""


def _operands(raw: str) -> list[str]:
    """`a # "b" # {c}` → its operands. A `#` inside braces or quotes is text."""
    parts: list[str] = []
    depth, quoted, start = 0, False, 0
    for i, ch in enumerate(raw):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == '"' and depth == 0:
            quoted = not quoted
        elif ch == "#" and depth == 0 and not quoted:
            parts.append(raw[start:i])
            start = i + 1
    parts.append(raw[start:])
    return [p.strip() for p in parts]


def _resolve_value(raw: str, strings: dict[str, str], string_errors: dict[str, str]) -> str:
    """A raw BibTeX value (`{X}`, `"X"`, `2020`, `jul`, `venue # " 2020"`) → its text."""
    out: list[str] = []
    for part in _operands(raw):
        if len(part) >= 2 and (part[0], part[-1]) in (("{", "}"), ('"', '"')):
            out.append(part[1:-1])
        elif part.isdigit():
            out.append(part)
        elif not _STRING_NAME_RE.fullmatch(part):
            raise _FieldError(f"cannot read the value {raw!r}")
        elif part.lower() in string_errors:
            raise _FieldError(f"@string `{part}` could not be read: {string_errors[part.lower()]}")
        elif part.lower() in strings:
            out.append(strings[part.lower()])
        else:
            raise _FieldError(f"undefined @string `{part}`")
    return "".join(out)


def _read_strings(library: Library) -> tuple[dict[str, str], dict[str, str]]:
    """`@string` name (lower-cased; BibTeX names are case-insensitive) → value, and → failure reason.

    Strings are resolved in file order against the month macros and the strings before them; a
    redefinition wins over the earlier one.
    """
    strings = dict(_MONTH_MACROS)
    errors: dict[str, str] = {}
    for block in library.blocks:
        if isinstance(block, DuplicateBlockKeyBlock):
            block = block.ignore_error_block
        if not isinstance(block, String):
            continue
        name = block.key.lower()
        try:
            strings[name] = _resolve_value(block.value, strings, errors)
            errors.pop(name, None)
        except _FieldError as e:
            strings.pop(name, None)
            errors[name] = str(e)
    return strings, errors


def _read_fields(
    entry: Entry, strings: dict[str, str], string_errors: dict[str, str]
) -> dict[str, str]:
    """An entry's fields as `{lower-cased key: decoded text}`; raises `_FieldError` naming the field."""
    fields: dict[str, str] = {}
    for field in entry.fields:
        key = field.key.lower()
        if key in fields:  # bibtexparser flags exact repeats; `Title` and `title` are one field too
            raise _FieldError(f"duplicate field `{key}`")
        try:
            fields[key] = decode_latex(_resolve_value(field.value, strings, string_errors))
        except (_FieldError, LatexDecodeError) as e:
            raise _FieldError(f"field `{field.key}`: {e}") from e
    return fields


class BibRecord(NamedTuple):
    """One `.bib` entry as read: values resolved and LaTeX-decoded, field keys lower-cased."""

    key: str
    entry_type: str  # lower-cased, e.g. "article"
    line: int        # 1-based line of the `@type{key` header
    fields: dict[str, str]


class BibRead(NamedTuple):
    records: list[BibRecord]
    failed: list[UnparsedEntry]   # entries bibtexparser split whose fields cannot be read
    parse_errors: dict[int, str]  # 1-based line → why bibtexparser could not split the block there


def _failed(entry: Entry, reason: str) -> UnparsedEntry:
    return UnparsedEntry(
        key=entry.key, entry_type=entry.entry_type.lower(), line=(entry.start_line or 0) + 1,
        reason=f"{reason}; {_NOT_CHECKED}",
    )


def read_bib_records(raw: str) -> BibRead:
    """Read every entry of `.bib` text. An entry that cannot be read is in `failed`, never guessed."""
    library = bibtexparser.parse_string(raw, parse_stack=[])
    strings, string_errors = _read_strings(library)
    records: list[BibRecord] = []
    failed: list[UnparsedEntry] = []
    parse_errors: dict[int, str] = {}
    for block in library.blocks:
        if isinstance(block, DuplicateFieldKeyBlock):
            dupes = ", ".join(f"`{k}`" for k in sorted(block.duplicate_keys))
            failed.append(_failed(block.ignore_error_block, f"duplicate field {dupes}"))
            continue
        # A repeated entry key: bibtexparser sets the later entry aside; it is read like any other.
        entry = block.ignore_error_block if isinstance(block, DuplicateBlockKeyBlock) else block
        if isinstance(entry, Entry):
            # `@article{key}`, or LaTeX such as `\@ifundefined{X}` in a mis-passed `.tex`.
            if not entry.fields:
                failed.append(_failed(entry, "the entry has no fields"))
                continue
            try:
                fields = _read_fields(entry, strings, string_errors)
            except _FieldError as e:
                failed.append(_failed(entry, str(e)))
                continue
            records.append(
                BibRecord(entry.key, entry.entry_type.lower(), (entry.start_line or 0) + 1, fields)
            )
        elif isinstance(block, ParsingFailedBlock) and not isinstance(block, DuplicateBlockKeyBlock):
            err = block.error
            reason = err.abort_reason if isinstance(err, BlockAbortedException) else str(err)
            parse_errors[(block.start_line or 0) + 1] = reason
    return BibRead(records, failed, parse_errors)


def _twin_from_raw(raw: str, key: str) -> BibEntry | None:
    """The commented-only entry `key`, read from the text with its lines' `%` markers removed."""
    m = re.search(r"(?m)^[^\n@]*@\w+\s*\{\s*" + re.escape(key) + r"(?=[,\s}])", raw)
    if not m:
        return None
    # The block ends, at the latest, where the next live entry starts.
    end = next(
        (h.start() for h in _ENTRY_LINE_RE.finditer(raw, m.end())
         if not re.search(r"(?<!\\)%", h.group("pre"))),
        len(raw),
    )
    uncommented = re.sub(r"(?m)^([ \t]*)%+", r"\1", raw[m.start():end])
    read = read_bib_records(uncommented)
    rec = next((r for r in read.records if r.key == key), None)
    if rec is None:
        return None
    return entry_from_fields(key, rec.entry_type, rec.fields, commented=True)


class BibParse(NamedTuple):
    entries: list[BibEntry]          # audited
    twins: list[BibEntry]            # commented-out entries (informational)
    unparsed: list[UnparsedEntry]    # live entries that could not be read (never audited)


def _brace_balance(text: str) -> int:
    """Net `{` minus `}` in `text`, ignoring escaped braces (`\\{`, `\\}`)."""
    unescaped = re.sub(r"\\[{}]", "", text)
    return unescaped.count("{") - unescaped.count("}")


def _unparsed_entries(
    raw: str, read_keys: set[str], commented_only: set[str], parse_errors: dict[int, str]
) -> list[UnparsedEntry]:
    """Live entry headers in `raw` that produced no entry, each with the likeliest reason.

    The block of an entry runs from its header to the next header. A block whose net brace count does
    not balance is reported as such; otherwise the reason is bibtexparser's own for the block at that
    line, or the generic one, never a guess.
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
        if key in read_keys or key in commented_only or key in reported:
            continue
        end = headers[i + 1].start() if i + 1 < len(headers) else len(raw)
        balance = _brace_balance(raw[m.start("type") - 1:end])
        line = raw.count("\n", 0, m.start()) + 1
        if balance > 0:
            reason = f"unbalanced braces ({balance} more '{{' than '}}')"
        elif balance < 0:
            reason = f"unbalanced braces ({-balance} more '}}' than '{{')"
        elif line in parse_errors:
            reason = f"bibtexparser could not parse this entry: {parse_errors[line]}"
        else:
            reason = "bibtexparser could not parse this entry"
        out.append(UnparsedEntry(
            key=key, entry_type=m.group("type").lower(), line=line,
            reason=f"{reason}; {_NOT_CHECKED}",
        ))
        reported.add(key)
    return out


def parse_bib(bib_path: str | Path) -> BibParse:
    """Parse a .bib file. Returns (audited_entries, commented_twins, unparsed_entries)."""
    return parse_bib_text(Path(bib_path).read_text(encoding="utf-8", errors="replace"))


def parse_bib_text(raw: str) -> BibParse:
    """Parse .bib text — a file's, or the records a PDF embeds (`pdf.embedded`)."""
    commented_only = _classify_keys(raw)
    read = read_bib_records(raw)

    entries = [
        entry_from_fields(r.key, r.entry_type, r.fields)
        for r in read.records if r.key not in commented_only
    ]
    twins: list[BibEntry] = []
    for key in dict.fromkeys(m.group("key") for m in _ENTRY_LINE_RE.finditer(raw)):
        if key in commented_only and (twin := _twin_from_raw(raw, key)) is not None:
            twins.append(twin)
    failed = [u for u in read.failed if u.key not in commented_only]

    read_keys = {r.key for r in read.records} | {u.key for u in read.failed}
    unparsed = failed + _unparsed_entries(raw, read_keys, commented_only, read.parse_errors)
    unparsed.sort(key=lambda u: u.line)
    return BibParse(entries, twins, unparsed)
