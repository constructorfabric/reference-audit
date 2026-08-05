r"""GROBID TEI XML → `BibEntry` + `CitationContext`. Pure: no I/O, no network.

Two things are recovered from one TEI document:

1. **The reference list** — `<back>//<listBibl>/<biblStruct>`, one per printed bibliography entry.
   Each becomes a `BibEntry` via `parsing.bib.entry_from_fields`, the same construction seam the
   `.bib` parser uses, so identifier normalization and venue/type selection are defined once.
2. **The in-text citations** — `<ref type="bibr" target="#bN">` markers inside body `<p>`s, resolved
   against the `biblStruct` `xml:id`s to give per-reference `CitationContext`s.

What this module refuses to do, per the project's reliability rules: it never guesses a link for an
unresolved marker, never invents a key, never returns a partial list from malformed XML, and records
*why* a reference could not be extracted instead of quietly emitting an empty record.

XML is parsed with the stdlib `xml.etree.ElementTree`, matching `sources.arxiv`. The input is TEI
serialized by our own GROBID from its internal model — not attacker-controlled markup passed through
from the PDF — so the entity-expansion hardening `defusedxml` adds has nothing to act on here.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from pydantic import BaseModel, Field

from reference_audit.models import BibEntry, CitationContext
from reference_audit.parsing.bib import entry_from_fields
from reference_audit.parsing.context import MIN_CONTEXT_CHARS, collapse, sentence_span

TEI_NS = "http://www.tei-c.org/ns/1.0"
XML_NS = "http://www.w3.org/XML/1998/namespace"

_TEI = f"{{{TEI_NS}}}"
_XML_ID = f"{{{XML_NS}}}id"

# A `level="m"` venue naming a proceedings volume rather than a book.
_PROCEEDINGS_RE = re.compile(
    r"\b(?:proc\.?|proceedings|conference|conf\.?|workshop|symposium|symp\.?|meeting)\b",
    re.IGNORECASE,
)

_DOI_URL_PREFIX_RE = re.compile(r"(?i)^\s*(?:https?://)?(?:dx\.)?doi\.org/")
_DOI_SHAPE_RE = re.compile(r"(?i)^10\.\d{4,9}/\S+$")
# Text that follows a DOI in a printed reference and is not part of it. Used to bound a DOI recovered
# from the raw reference string after whitespace removal has glued the next word onto it.
_DOI_STOP_RE = re.compile(r"(?i)(URL|https?://|ISBN|ISSN|Accessed|arXiv|In:|pp\.)")
# How far a line-break repair may extend a DOI. Real suffixes that get split are short (".ch9",
# ".v37i12.26698", ".2007.06.007"); anything longer means the extension has run past the end of the
# DOI into surrounding prose — which happens when GROBID merges two references into one record, where
# the "surrounding prose" is the whole next reference. Without this bound the repair turns a correct
# DOI into a fabricated one, which the compiled-PDF oracle caught.
_DOI_MAX_EXTENSION = 30
_DOI_MAX_LENGTH = 80


class TeiParseError(ValueError):
    """The TEI could not be parsed as XML at all.

    Distinct from "parsed but empty": a truncated or malformed document must never yield a partial
    reference list that would then be reported as if it were the whole bibliography.
    """


class TeiReferences(BaseModel):
    """Everything one TEI document yields, before any network identification.

    The counters exist so the caller can tell the difference between "this document cites nothing"
    and "GROBID could not link this document's citations" — reported as unknown, never as zero.
    """

    entries: list[BibEntry] = Field(default_factory=list)
    contexts: dict[str, list[CitationContext]] = Field(default_factory=dict)
    # `<ref target="#bN">` pointing at an id no `biblStruct` declares — malformed TEI, normally empty.
    dangling_targets: list[str] = Field(default_factory=list)
    # Markers GROBID recognized as citations but could not attach to a bibliography item. Counted so
    # they can be reported; never guessed into a key.
    unresolved_markers: int = 0
    # Total `type="bibr"` markers seen in the body. Zero means marker→bibliography linking produced
    # nothing at all, so citedness for this document is unknown rather than "nothing is cited".
    markers_seen: int = 0
    # key → reasons this specific reference came out incomplete, surfaced as per-entry issues.
    extraction_failures: dict[str, list[str]] = Field(default_factory=dict)

    def linked_keys(self) -> set[str]:
        return set(self.contexts)


def _text(el: ET.Element | None) -> str:
    """All text under an element, whitespace-collapsed (TEI wraps prose in inline markup)."""
    if el is None:
        return ""
    return collapse(" ".join(el.itertext()))


def _person_name(persname: ET.Element) -> str:
    """A `<persName>` rendered in BibTeX's own `"Surname, Forename(s)"` convention.

    TEI labels the parts explicitly (`<surname>`, `<forename>`), so this is a faithful re-ordering
    rather than a guess — and it matters beyond cosmetics: `compute_content_hash` hashes the author
    strings as written, so emitting the printed order ("Andrey Bagrov") instead of the `.bib` order
    ("Bagrov, Andrey") would give the same work two different content hashes depending on whether it
    was read from a PDF or a `.bib`, and the two inputs could not share a cached verdict.

    With no `<surname>` to key on (an organizational author, or a name GROBID could not split) the
    parts are joined in document order — the only honest option.
    """
    surname = collapse(" ".join(el.text or "" for el in persname.findall(f"{_TEI}surname")))
    forenames = [
        collapse(el.text or "")
        for el in persname.findall(f"{_TEI}forename")
        if collapse(el.text or "")
    ]
    if surname:
        return f"{surname}, {' '.join(forenames)}" if forenames else surname
    parts = [collapse(child.text or "") for child in persname if collapse(child.text or "")]
    return " ".join(parts)


def _resolve_doi(extracted: str, raw: str) -> tuple[str | None, str | None]:
    """Return (doi, note) for a DOI GROBID read, extending it if the PDF broke it across a line.

    A DOI printed across a line break loses its tail: GROBID reads `10.1016/j.cognition` from a
    reference whose text layer says `10.101 6/j.cognition.2007.06.007`. That prefix is not a shorter
    form of the right DOI — it is a *different* identifier that resolves to another document or to
    nothing, which is exactly the kind of confidently-wrong answer this tool exists to prevent.

    The rule is deliberately extend-only. We look for the longest DOI-shaped token in the reference
    text (whitespace removed, so a mid-DOI line break closes up) that *begins with* what GROBID
    returned, and take it only if it is strictly longer. So the outcome is either GROBID's own value or
    a superset of it — never a competing DOI, and never a discarded one. A repair is reported, so a
    reader can check it against the PDF.

    Getting this wrong in the other direction is easy and was caught by the compiled-PDF oracle: an
    earlier version treated the sentence-ending period after a *complete* DOI as evidence of
    truncation and threw good identifiers away.
    """
    doi = _DOI_URL_PREFIX_RE.sub("", extracted).strip().rstrip(".,;:)]")
    if not doi or not raw:
        return (doi or None), None

    despaced = re.sub(r"[\s­‐‑]+", "", raw)
    at = despaced.lower().find(doi.lower())
    if at == -1:
        return doi, None

    # Only characters that can legitimately continue a DOI, so a following ", 2020" or "; see" ends
    # the token rather than being absorbed into it.
    ext = re.match(r"[0-9A-Za-z._()/:\-]*", despaced[at + len(doi) :]).group(0)
    stop = _DOI_STOP_RE.search(ext)
    if stop:
        ext = ext[: stop.start()]
    if len(ext) > _DOI_MAX_EXTENSION:
        return doi, None  # ran past the DOI into prose — trust what GROBID read
    candidate = (doi + ext).rstrip(".,;:)]")

    if (
        len(candidate) > len(doi)
        and len(candidate) <= _DOI_MAX_LENGTH
        and _DOI_SHAPE_RE.match(candidate)
    ):
        return candidate, (
            f"GROBID's DOI was cut short by a line break in the PDF (it read '{doi}'); extended to "
            f"'{candidate}' from the reference text — verify against the PDF if the match looks wrong"
        )
    return doi, None


def _idno(bs: ET.Element, kind: str) -> str:
    for el in bs.iter(f"{_TEI}idno"):
        if (el.get("type") or "").lower() == kind.lower():
            value = _text(el)
            if value:
                return value
    return ""


def _biblscope(bs: ET.Element, unit: str) -> str:
    """A `<biblScope unit="...">`: prefer the `@from`-`@to` range, else the element text."""
    for el in bs.iter(f"{_TEI}biblScope"):
        if (el.get("unit") or "") != unit:
            continue
        lo, hi = el.get("from"), el.get("to")
        if lo and hi:
            return f"{lo}--{hi}"
        if lo:
            return lo
        value = _text(el)
        if value:
            return value
    return ""


def _year(bs: ET.Element) -> str:
    """The publication year, preferring an explicitly `type="published"` date."""
    for want_published in (True, False):
        for el in bs.iter(f"{_TEI}date"):
            if want_published and (el.get("type") or "") != "published":
                continue
            when = (el.get("when") or "").strip()
            if when[:4].isdigit():
                return when[:4]
            text = _text(el)
            m = re.search(r"\b(1\d{3}|20\d{2}|21\d{2})\b", text)
            if m:
                return m.group(1)
    return ""


def _titles(bs: ET.Element) -> tuple[str, str, str]:
    """Split the `<title>` elements into (title, journal, booktitle).

    `<analytic>/<title>` is the work's own title whenever it is present. The `<monogr>` titles then
    depend on whether an analytic title exists, and getting this wrong breaks matching in both
    directions:

    * `level="j"` is a journal name — always a venue, never a work title.
    * `level="m"` is a monograph. WITH an analytic title the reference is a contribution *inside* that
      monograph, so the monograph is the venue (`booktitle`) — this is what stops a book-series name
      from being read as a chapter's title. WITHOUT one, the reference *is* the monograph: a cited
      book has no analytic title, and treating its title as a venue would leave the entry title-less
      and unmatchable, which is exactly what the compiled-PDF oracle caught.
    """
    analytic = _text(bs.find(f"{_TEI}analytic/{_TEI}title"))
    title = analytic
    journal = booktitle = ""
    for el in bs.findall(f"{_TEI}monogr/{_TEI}title"):
        value = _text(el)
        if not value:
            continue
        level = el.get("level") or ""
        if level == "j":
            journal = journal or value
        elif level == "m":
            if analytic:
                booktitle = booktitle or value
            elif not title:
                title = value
        elif not title:
            title = value
    return title, journal, booktitle


def _infer_bib_type(bs: ET.Element, fields: dict[str, str]) -> str:
    """Infer a BibTeX entry type from TEI structure.

    TEI carries no entry type, but `entry_type` decides which source adapters a reference is routed
    to (`sources.registry.route_entry`) and which deterministic issues apply. Leaving everything
    UNKNOWN would silently send books to article aggregators, so structure is used where it is
    unambiguous and UNKNOWN is returned when it is not — UNKNOWN routes to the general article-ish
    adapter set, which is the right default for an unclassifiable reference.
    """
    if fields.get("journal"):
        return "article"
    booktitle = fields.get("booktitle", "")
    if bs.find(f".//{_TEI}meeting") is not None or _PROCEEDINGS_RE.search(booktitle):
        return "inproceedings"
    has_book_signal = bool(fields.get("isbn") or fields.get("publisher"))
    has_analytic = bool(_text(bs.find(f"{_TEI}analytic/{_TEI}title")))
    if has_book_signal and not has_analytic:
        return "book"
    if has_analytic and booktitle:
        return "incollection"
    if fields.get("url") and not booktitle and not has_book_signal:
        return "misc"
    return ""


def _fields_from_biblstruct(bs: ET.Element) -> tuple[dict[str, str], list[str], list[str]]:
    """One `<biblStruct>` → (BibTeX-shaped fields, author list, extraction failures)."""
    title, journal, booktitle = _titles(bs)
    authors = [
        name
        for name in (_person_name(p) for p in bs.iter(f"{_TEI}persName"))
        if name
    ]
    raw = _text(bs.find(f"{_TEI}note[@type='raw_reference']"))

    fields: dict[str, str] = {}
    if title:
        fields["title"] = title
    if journal:
        fields["journal"] = journal
    if booktitle:
        fields["booktitle"] = booktitle
    year = _year(bs)
    if year:
        fields["year"] = year
    for name, value in (
        ("publisher", _text(bs.find(f".//{_TEI}imprint/{_TEI}publisher"))),
        ("pages", _biblscope(bs, "page")),
        ("volume", _biblscope(bs, "volume")),
        ("number", _biblscope(bs, "issue")),
        ("isbn", _idno(bs, "ISBN")),
        ("pmid", _idno(bs, "PMID")),
    ):
        if value:
            fields[name] = value

    # A DOI is stored already bare. `pipeline._parse_issues` advises "consider citing the bare DOI"
    # when raw_fields["doi"] looks like a URL — sound advice for a .bib the author can fix, but for a
    # PDF the `https://doi.org/...` form is GROBID's rendering of the printed reference, not an
    # authoring choice. Normalizing here makes that advice structurally unable to misfire.
    doi_note: str | None = None
    if _idno(bs, "DOI"):
        doi, doi_note = _resolve_doi(_idno(bs, "DOI"), raw)
        if doi:
            fields["doi"] = doi
    arxiv = _idno(bs, "arXiv")
    if arxiv:
        fields["eprint"] = arxiv
        fields["archiveprefix"] = "arXiv"
    # `<ptr>` is not reliably tagged `type="url"` — GROBID emits a bare `<ptr target="https://…"/>`
    # for the URL printed in a reference. Keying on the attribute (as the sciwrite-lint client does)
    # silently drops those, and with them the Google Books volume ids that are often the only
    # identifier a cited book has. So accept any `<ptr>` whose target is an http(s) URL.
    for ptr in bs.iter(f"{_TEI}ptr"):
        target = (ptr.get("target") or "").strip()
        if target.lower().startswith(("http://", "https://")):
            fields["url"] = target
            break
    if raw:
        fields["grobid_raw"] = raw

    failures: list[str] = []
    if doi_note:
        failures.append(doi_note)
    quoted = f' (raw: "{raw[:200]}")' if raw else " (GROBID captured no raw reference string either)"
    if not title:
        failures.append(f"GROBID extracted no title for this reference{quoted}")
    if not authors:
        failures.append(
            f"GROBID extracted no author list for this reference{quoted} — "
            "matching is title-only and may be ambiguous"
        )
    return fields, authors, failures


def _keys_for(bibl_structs: list[ET.Element]) -> list[str]:
    """Per-`biblStruct` entry keys, from the TEI `xml:id` that `<ref target="#...">` points at.

    A PDF has no BibTeX keys, so the id GROBID assigned is the only stable handle — and it is exactly
    what the in-text markers reference, which is what makes citation linking possible. Missing ids
    fall back to position; duplicates get a suffix so no two entries collide (which would silently
    merge two references in the report).
    """
    keys: list[str] = []
    seen: dict[str, int] = {}
    for i, bs in enumerate(bibl_structs):
        key = (bs.get(_XML_ID) or "").strip() or f"b{i}"
        count = seen.get(key, 0)
        seen[key] = count + 1
        keys.append(key if count == 0 else f"{key}-{count + 1}")
    return keys


def _flatten_paragraph(p_el: ET.Element) -> tuple[str, list[tuple[int, str | None]]]:
    r"""Plain text of one `<p>` with citation markers REMOVED, plus (offset, target) for each.

    The marker's own surface text (`[13]`, `(Chan, 2019)`) is deliberately dropped: it is not part of
    the citing claim, exactly as the LaTeX path substitutes the `\cite` macro away before extracting a
    sentence. The offset where it stood is kept so `sentence_span` can be asked for the surrounding
    sentence via a zero-width span.

    Whitespace is NOT collapsed here — that would invalidate the offsets. The extracted span is
    collapsed at the end, as the LaTeX path does.
    """
    buf: list[str] = []
    markers: list[tuple[int, str | None]] = []
    pos = 0

    def emit(s: str | None) -> None:
        nonlocal pos
        if s:
            buf.append(s)
            pos += len(s)

    emit(p_el.text)
    for child in p_el:
        if child.tag == f"{_TEI}ref" and (child.get("type") or "") == "bibr":
            target = (child.get("target") or "").lstrip("#").strip() or None
            markers.append((pos, target))
        else:
            emit(" ".join(child.itertext()))
        emit(child.tail)
    return "".join(buf), markers


def _body_paragraphs(root: ET.Element) -> list[ET.Element]:
    """Prose `<p>`s of the article body, each visited exactly once, in document order.

    Descends only through `<div>`, so the abstract, figure captions, tables and the `<back>`
    bibliography are excluded. Note `div.findall(p)` rather than `div.iter(ref)`: iterating refs from
    each div would visit a marker in a nested div once per ancestor, inflating every ordinal.
    """
    body = root.find(f".//{_TEI}text/{_TEI}body")
    if body is None:
        return []
    paragraphs = list(body.findall(f"{_TEI}p"))
    for div in body.iter(f"{_TEI}div"):
        paragraphs.extend(div.findall(f"{_TEI}p"))
    return paragraphs


def parse_tei(xml_text: str) -> TeiReferences:
    """Map one GROBID TEI document to entries + citing contexts.

    Raises `TeiParseError` if the XML is malformed. An empty `entries` list is a legitimate (if
    unusable) result the caller must handle — it means GROBID found no bibliography.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise TeiParseError(f"GROBID returned XML that could not be parsed: {exc}") from exc

    bibl_structs = [
        bs
        for listbibl in root.iter(f"{_TEI}listBibl")
        for bs in listbibl.findall(f"{_TEI}biblStruct")
    ]
    keys = _keys_for(bibl_structs)

    result = TeiReferences()
    id_to_key: dict[str, str] = {}
    for index, (bs, key) in enumerate(zip(bibl_structs, keys, strict=True)):
        fields, authors, failures = _fields_from_biblstruct(bs)
        fields["grobid_ref_index"] = str(index + 1)   # 1-based, as printed
        fields["grobid_tei_id"] = (bs.get(_XML_ID) or "").strip()
        entry = entry_from_fields(
            key, _infer_bib_type(bs, fields), fields, authors=authors
        )
        result.entries.append(entry)
        if failures:
            result.extraction_failures[key] = failures
        tei_id = (bs.get(_XML_ID) or "").strip()
        if tei_id:
            id_to_key.setdefault(tei_id, key)

    counts: dict[str, int] = {}
    for p_el in _body_paragraphs(root):
        text, markers = _flatten_paragraph(p_el)
        for pos, target in markers:
            result.markers_seen += 1
            if target is None:
                result.unresolved_markers += 1
                continue
            key = id_to_key.get(target)
            if key is None:
                result.dangling_targets.append(target)
                continue
            left, right = sentence_span(text, pos, pos)
            cleaned = collapse(text[left:right])
            if len(cleaned) < MIN_CONTEXT_CHARS:
                # fold in the preceding sentence so there is an actual claim to judge
                prev_left, _ = sentence_span(text, max(left - 2, 0), max(left - 1, 0))
                cleaned = collapse(text[prev_left:right])
            if not cleaned:
                # No prose around the marker (a bare citation in a caption-like fragment). Counting it
                # as unresolved is honest; emitting an empty context would give the LLM nothing to
                # judge while looking like a real citing claim.
                result.unresolved_markers += 1
                continue
            ordinal = counts.get(key, 0)
            counts[key] = ordinal + 1
            result.contexts.setdefault(key, []).append(
                CitationContext(key=key, text=cleaned, ordinal=ordinal, command="bibr")
            )

    result.dangling_targets = sorted(set(result.dangling_targets))
    return result
