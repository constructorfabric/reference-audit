"""Read Word bibliography sources and CITATION fields without Word or network access.

Only authored bibliography XML is accepted. Formatted bibliography paragraphs are never guessed
back into metadata. An unreadable package/part fails the input; an unreadable source is reported
individually, allowing the other sources to proceed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile

from loguru import logger

from reference_audit.models import BibEntry, CitationContext, UnparsedEntry
from reference_audit.parsing.bib import entry_from_fields
from reference_audit.parsing.context import MIN_CONTEXT_CHARS, collapse, sentence_span
from reference_audit.parsing.identifiers import extract_arxiv_id, normalize_doi, normalize_isbn13

_B = "{http://schemas.openxmlformats.org/officeDocument/2006/bibliography}"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_TYPES = {
    "JournalArticle": "article",
    "ArticleInAPeriodical": "article",
    "ConferenceProceedings": "inproceedings",
    "Book": "book",
    "BookSection": "incollection",
    "Report": "techreport",
    "DocumentFromInternetSite": "online",
    "InternetSite": "online",
    "ElectronicSource": "electronic",
    "Misc": "misc",
}
_FIELDS = {
    "Title": "title", "Year": "year", "JournalName": "journal",
    "BookTitle": "booktitle", "ConferenceName": "howpublished", "Publisher": "publisher",
    "Pages": "pages", "Volume": "volume", "Issue": "number", "Edition": "edition",
    "DOI": "doi", "URL": "url", "City": "address",
}
_STORY = re.compile(r"word/(?:document|footnotes|endnotes|header\d+|footer\d+)\.xml\Z")
_MAX_PART_BYTES = 32 * 1024 * 1024
DOCX_SCOPE_WARNING = (
    "WARNING: WORD AUDITS CHECK ONLY THE STRUCTURED BIBLIOGRAPHY.\n"
    "Free-form / manually typed references are NOT checked and may be missed, even when this\n"
    "document also contains structured sources. Only native Word bibliography sources and\n"
    "CITATION fields are supported; Zotero/Mendeley fields are not interpreted."
)


class DocxInputError(ValueError):
    """The Word input cannot supply a reliable structured bibliography or citation list."""


@dataclass
class DocxParse:
    entries: list[BibEntry] = field(default_factory=list)
    unparsed: list[UnparsedEntry] = field(default_factory=list)
    cited_keys: set[str] = field(default_factory=set)
    contexts: dict[str, list[CitationContext]] = field(default_factory=dict)
    context_failures: dict[str, list[str]] = field(default_factory=dict)
    citation_fields: int = 0


def _read_xml(package: ZipFile, name: str) -> ET.Element:
    info = package.getinfo(name)
    if info.file_size > _MAX_PART_BYTES:
        raise DocxInputError(f"{name} exceeds the {_MAX_PART_BYTES // 1024 // 1024} MiB XML part limit")
    data = package.read(name)
    # Word parts do not need DTDs; reject them rather than expand untrusted entities.
    if b"<!DOCTYPE" in data.upper().replace(b"\x00", b""):
        raise DocxInputError(f"{name} contains an unsupported XML document type declaration")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise DocxInputError(f"cannot read XML part {name}: {exc}") from exc


def _value(source: ET.Element, name: str) -> str:
    nodes = source.findall(_B + name)
    if len(nodes) > 1:
        raise ValueError(f"duplicate {name} fields")
    return collapse("".join(nodes[0].itertext())) if nodes else ""


def _source_entry(source: ET.Element, key: str) -> BibEntry:
    fields = {target: value for name, target in _FIELDS.items() if (value := _value(source, name))}
    kind = _value(source, "SourceType")
    fields["word_source_type"] = kind
    standard = _value(source, "StandardNumber")
    if standard:
        fields["word_standard_number"] = standard
        if doi := normalize_doi(standard):
            fields.setdefault("doi", doi)
        elif arxiv := extract_arxiv_id(None, fallback_text=standard):
            fields["eprint"] = arxiv
            fields["archiveprefix"] = "arXiv"
        elif isbn := normalize_isbn13(standard):
            fields["isbn"] = isbn
    if "year" in fields and not re.fullmatch(r"\d{4}", fields["year"]):
        raise ValueError(f"unreadable Year {fields['year']!r}")

    authors: list[str] = []
    groups = source.findall(f"{_B}Author/{_B}Author")
    if len(groups) > 1:
        raise ValueError("duplicate author groups")
    if groups:
        group = groups[0]
        corporate = _value(group, "Corporate")
        people = group.findall(f"{_B}NameList/{_B}Person")
        if corporate and people:
            raise ValueError("author group contains both Corporate and Person names")
        if corporate:
            authors = [corporate]
        else:
            for person in people:
                last = _value(person, "Last")
                given = collapse(f"{_value(person, 'First')} {_value(person, 'Middle')}")
                if not last:
                    raise ValueError("a Person author has no Last name; author list was not guessed")
                authors.append(f"{last}, {given}" if given else last)
            if not people:
                raise ValueError("unreadable author group (expected Corporate or NameList/Person)")
    return entry_from_fields(key, _TYPES.get(kind, kind or "unknown"), fields, authors=authors)


def _citation_keys(instruction: str) -> list[str]:
    tokens = re.findall(r'"[^"]*"|[^\s]+', instruction)
    if not tokens or tokens[0].upper() != "CITATION":
        return []
    if len(tokens) < 2 or tokens[1].startswith("\\"):
        raise DocxInputError(f"CITATION field has no source tag: {instruction!r}")
    keys = [tokens[1].strip('"')]
    i = 2
    while i < len(tokens):
        switch = tokens[i].lower()
        if switch in {"\\m", "\\l", "\\p", "\\s", "\\v", "\\f", "\\*"}:
            if i + 1 >= len(tokens) or tokens[i + 1].startswith("\\"):
                raise DocxInputError(f"CITATION field switch lacks an argument: {instruction!r}")
            if switch == "\\m":
                keys.append(tokens[i + 1].strip('"'))
            i += 2
        elif switch in {"\\t", "\\y", "\\n"}:
            i += 1
        else:
            raise DocxInputError(f"unsupported CITATION field syntax: {instruction!r}")
    if any(not key for key in keys):
        raise DocxInputError(f"CITATION field has an empty source tag: {instruction!r}")
    return list(dict.fromkeys(keys))


@dataclass
class _Field:
    instruction: str = ""
    result: bool = False
    recorded: bool = False


def _read_citations(root: ET.Element, parsed: DocxParse, part: str) -> None:
    """Walk simple and complex fields, including instructions split across runs/paragraphs.

    Citation display results are removed before sentence extraction; bibliography results are
    suppressed entirely. The field stack persists across paragraphs, as Word permits.
    """
    stack: list[_Field] = []
    text: list[str] = []
    occurrences: list[tuple[int, list[str]]] = []
    offset = 0

    def record(current: _Field) -> None:
        if current.recorded:
            return
        current.recorded = True
        # Nested fields in a bibliography or another field result are not in-text citations.
        if len(stack) > 1:
            return
        keys = _citation_keys(current.instruction)
        if keys:
            parsed.citation_fields += 1
            parsed.cited_keys.update(keys)
            occurrences.append((offset, keys))

    def suppressed() -> bool:
        return any(
            not item.result or re.match(r"\s*(?:CITATION|BIBLIOGRAPHY)\b", item.instruction, re.I)
            for item in stack
        )

    def walk(node: ET.Element) -> None:
        nonlocal offset
        if node.tag in {_W + "del", _W + "moveFrom"}:
            return
        if node.tag == _W + "fldSimple":
            current = _Field(node.get(_W + "instr", ""), result=True)
            stack.append(current)
            record(current)
            for child in node:
                walk(child)
            stack.pop()
            return
        if node.tag == _W + "fldChar":
            kind = node.get(_W + "fldCharType")
            if kind == "begin":
                stack.append(_Field())
            elif kind in {"separate", "end"}:
                if not stack:
                    raise DocxInputError(f"unmatched field {kind} in {part}")
                record(stack[-1])
                if kind == "separate":
                    stack[-1].result = True
                else:
                    stack.pop()
            return
        if node.tag == _W + "instrText":
            if stack and not stack[-1].result:
                stack[-1].instruction += node.text or ""
            return
        if node.tag in {_W + "t", _W + "tab", _W + "br", _W + "cr"} and not suppressed():
            value = (node.text or "") if node.tag == _W + "t" else " "
            text.append(value)
            offset += len(value)
        for child in node:
            walk(child)
        if node.tag == _W + "p" and not suppressed():
            text.append("\n\n")
            offset += 2

    walk(root)
    if stack:
        raise DocxInputError(f"unclosed Word field in {part}; citation linking cannot be trusted")
    prose = "".join(text)
    for position, keys in occurrences:
        left, right = sentence_span(prose, position, position)
        context = collapse(prose[left:right])
        if len(context) < MIN_CONTEXT_CHARS:
            prev_left, _ = sentence_span(prose, max(left - 2, 0), max(left - 1, 0))
            context = collapse(prose[prev_left:right])
        for key in keys:
            if not context:
                parsed.context_failures.setdefault(key, []).append(
                    "native Word citation field has no surrounding prose; citation alignment "
                    "cannot be checked for this occurrence"
                )
                continue
            contexts = parsed.contexts.setdefault(key, [])
            contexts.append(CitationContext(
                key=key, text=context, ordinal=len(contexts), command="CITATION",
            ))


def parse_docx(path: str | Path) -> DocxParse:
    """Read a .docx with Word's native bibliography. No conversion or external services."""
    path = Path(path)
    parsed = DocxParse()
    try:
        with ZipFile(path) as package:
            names = package.namelist()
            if len(names) != len(set(names)):
                raise DocxInputError("duplicate package part names; the Word input is ambiguous")
            if "word/document.xml" not in names:
                raise DocxInputError("missing word/document.xml; expected an OOXML .docx file")
            sources: list[ET.Element] = []
            for name in sorted(names):
                if re.fullmatch(r"customXml/[^/]+\.xml", name):
                    root = _read_xml(package, name)
                    if root.tag == _B + "Sources":
                        sources.extend(root.findall(_B + "Source"))
            if not sources:
                raise DocxInputError(
                    f"No structured Word bibliography sources found in {path.name}. "
                    "Use Word's References / Manage Sources and Insert Citation, or supply a .bib "
                    "or PDF. Plain-text bibliographies, Zotero/Mendeley fields and legacy .doc "
                    "files are not supported by this importer."
                )
            # Ambiguous tags cannot reliably identify either record. Report every affected source.
            tags = [collapse(s.findtext(_B + "Tag", default="")) for s in sources]
            counts = Counter(tags)
            for index, (source, tag) in enumerate(zip(sources, tags), 1):
                try:
                    if not tag:
                        raise ValueError("missing source Tag")
                    if counts[tag] > 1:
                        raise ValueError(f"duplicate source Tag {tag!r}; citation target is ambiguous")
                    if _value(source, "Tag") != tag:
                        raise ValueError("inconsistent source Tag")
                    parsed.entries.append(_source_entry(source, tag))
                except ValueError as exc:
                    reason = f"Word source {index}: {exc}; this source was not checked"
                    logger.warning("{}: {}", path.name, reason)
                    parsed.unparsed.append(UnparsedEntry(
                        key=tag or f"word-source-{index}",
                        entry_type=source.findtext(_B + "SourceType", default=""), reason=reason,
                    ))
            for name in sorted(names):
                if _STORY.fullmatch(name):
                    _read_citations(_read_xml(package, name), parsed, name)
    except (BadZipFile, OSError, KeyError, RuntimeError, NotImplementedError) as exc:
        raise DocxInputError(f"Cannot read Word file {path}: {exc}") from exc
    return parsed
