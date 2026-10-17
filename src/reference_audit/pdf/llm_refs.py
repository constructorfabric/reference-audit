r"""A PDF's printed reference list as read by an LLM: the output schema, and the pure checks and
mappings around it.

The network call is `llm.client.LLMClient.read_pdf`. Everything here is pure except
`pdf_text_layer`, which reads the local file. The design was measured before it was adopted (see
`docs/archive/llm-pdf-references.md`):

* The schema constrains identifiers and the year with regex and range limits, which structured
  outputs enforce while decoding. Every constrained field is nullable, and the instructions say to use
  null rather than repair. A constraint guarantees the shape of a value, not that it was printed: a
  model with no escape hatch writes a well-formed wrong value (a space in a DOI became `%20`).
* A model can stop early and still return a completed, schema-valid response. So the list names
  its own last printed entry *before* transcribing, and reports its coverage *after*. `list_problems`
  rejects a list whose last entry is not the one it named, or which says it stopped early.
* A DOI can be copied from another entry, or recalled from memory. `doi_problems` requires every
  DOI to occur in the PDF's own text layer, and no DOI to be shared by two entries with different
  titles.

Entries are built through `parsing.bib.entry_from_fields`, the same seam the `.bib` parser and the
TEI mapper use.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from anyascii import anyascii
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from reference_audit.matching.features import title_ratio
from reference_audit.matching.names import same_person
from reference_audit.models import BibEntry
from reference_audit.parsing.bib import entry_from_fields

# Keys the extraction cache together with the PDF and the model: bump it whenever the schema or the
# instructions change, so a stored reading made under other rules is never reused.
SCHEMA_VERSION = "1"


class PdfReadError(RuntimeError):
    """Neither model produced a usable reading of a PDF's reference list. The message names each
    model's reason; the document is not audited from a partial list."""


DOI_PATTERN = r"^10\.[0-9]{4,9}/\S+$"
ARXIV_PATTERN = r"^([0-9]{4}\.[0-9]{4,5}|[a-z][a-z.\-]*/[0-9]{7})(v[0-9]+)?$"
ISBN_PATTERN = r"^[0-9][0-9\- ]{8,15}[0-9Xx]$"
URL_PATTERN = r"^https?://\S+$"


class PrintedReference(BaseModel):
    """One entry of the printed reference list, transcribed — not looked up, not completed."""

    # Output follows schema key order, and this docstring is sent as the schema description, so the
    # rationale lives in comments: `raw_text` comes first so the fields are read off the transcription.
    raw_text: str = Field(description="The whole entry exactly as printed, joined into one line.")
    label: str | None = Field(description="Printed label without brackets or dot ('12'), or null.")
    entry_type: Literal[
        "article", "inproceedings", "book", "incollection", "thesis", "report", "misc"
    ]
    title: str | None = Field(description="The printed title, or null if the style prints none.")
    authors: list[str] = Field(
        description="Printed person or organization names, in order; never 'et al.'."
    )
    authors_truncated: bool = Field(description="True if 'et al.' or an ellipsis is printed.")
    year: int | None = Field(ge=1000, le=2999)
    venue: str | None = Field(description="Journal, proceedings or series as printed.")
    publisher: str | None
    volume: str | None
    issue: str | None
    pages: str | None
    doi: str | None = Field(
        pattern=DOI_PATTERN,
        description="The bare DOI printed in this entry; null if none, or if what is printed is not "
        "a well-formed DOI.",
    )
    arxiv_id: str | None = Field(
        pattern=ARXIV_PATTERN, description="An arXiv id printed in this entry, without 'arXiv:'."
    )
    isbn: str | None = Field(pattern=ISBN_PATTERN, description="An ISBN printed in this entry.")
    url: str | None = Field(
        pattern=URL_PATTERN, description="A printed URL that is not a DOI or arXiv link."
    )
    doubts: list[str] = Field(
        description="One item per judgment that could change a value: a field left null because "
        "it is printed malformed, a line break inside an identifier or a name, an unclear entry "
        "boundary. Never ordinary word hyphenation."
    )


class PrintedReferenceList(BaseModel):
    """The document's printed reference list."""

    has_reference_list: bool = Field(
        description="False when the document prints no bibliography or reference list."
    )
    # Before `references`: where the list ends is settled before transcribing, so a list that stops
    # early disagrees with it. After `references`: a report on what was written, not a promise.
    last_printed_entry_start: str | None = Field(
        description="The first ten or so words of the LAST entry of the printed reference list, "
        "verbatim; null if there is no reference list."
    )
    references: list[PrintedReference]
    coverage: Literal["all_entries", "stopped_early"] = Field(
        description="all_entries only if references contains every printed entry through the last "
        "one; stopped_early otherwise."
    )
    problems: list[str] = Field(
        description="Problems with the list as a whole (unreadable pages, unclear order)."
    )


INSTRUCTIONS = """\
You transcribe the reference list (bibliography) of the attached PDF into structured records.

Rules:
- Transcribe only what is printed in the document's reference list. Never look anything up, never
  correct, complete or normalize a reference from your own knowledge, never add a DOI, arXiv id, year
  or author that is not printed in that entry. A field that is not printed is null.
- A reference list is a References/Bibliography/Literature section, an unheaded numbered list at the
  end, or reference footnotes. Citations that appear only inline in the body text are not part of it.
  If the document has no reference list, return has_reference_list=false and an empty list.
- First find where the printed reference list ends and copy the start of its last entry into
  last_printed_entry_start. Then transcribe every entry, in printed reading order (in a two-column
  layout: left column top to bottom, then right column), through that last entry. Do not merge two
  entries or split one. Set coverage to stopped_early if you did not transcribe them all.
- raw_text first, then read every other field off it.
- authors: one string per printed person or organization, as printed ("Smith, J." or "J. Smith").
  Do not include "et al."; set authors_truncated instead.
- doi: the bare DOI (10.xxxx/...) as printed, also when printed as a https://doi.org/ link. A DOI
  broken across lines is joined without the line-break hyphen only if the hyphen is clearly not part
  of the DOI; record the judgment in that entry's doubts.
- doi, arxiv_id, isbn, url and year have a fixed format. If what is printed does not fit it, set the
  field to null and say why in doubts. Never repair a value into a well-formed one you did not see
  printed; joining a value that a line break split is the only allowed change. If an entry prints
  the same identifier twice in different forms (a "doi:" field and a doi.org URL), use the
  well-formed one and record the difference in doubts.
- Doubts about one entry go in its doubts; problems is only for the list as a whole. Do not report
  ordinary words hyphenated at a line end; report a line break only when it falls inside an
  identifier, a URL or a name and the hyphen could belong to it.
"""

USER_PROMPT = "Transcribe this document's reference list."

# A printed label at the start of an entry ("[12]", "12.", "(12)"), dropped before comparing starts.
_LABEL_RE = re.compile(r"^\s*[\[(]?\s*[0-9]{1,4}\s*[\]).]?\s+")
# How many normalized characters of the named last entry must agree with the last transcribed one.
_ANCHOR_CHARS = 40
_ANCHOR_MIN_RATIO = 90


def _norm_text(text: str) -> str:
    """Lower-case ASCII letters and digits: spacing, hyphenation, accents and punctuation fall away."""
    return re.sub(r"[^a-z0-9]", "", anyascii(_LABEL_RE.sub("", text or "")).lower())


def list_problems(result: PrintedReferenceList) -> list[str]:
    """Why this reading cannot be used as the document's reference list (empty when it can).

    A list is usable only if it is internally consistent and complete by its own account: it covers
    every entry, and its last entry is the last printed entry it named before transcribing.
    """
    n = len(result.references)
    if not result.has_reference_list:
        return [f"it says the document has no reference list but returned {n} entries"] if n else []
    if n == 0:
        return ["it says the document has a reference list but returned no entries"]
    problems: list[str] = []
    if result.coverage != "all_entries":
        said = f" ({'; '.join(result.problems)})" if result.problems else ""
        problems.append(f"it reports that it stopped before the end of the reference list{said}")
    anchor = _norm_text(result.last_printed_entry_start or "")[:_ANCHOR_CHARS]
    if not anchor:
        problems.append("it did not name the last printed entry")
    else:
        last = _norm_text(result.references[-1].raw_text)[: len(anchor)]
        if fuzz.ratio(last, anchor) < _ANCHOR_MIN_RATIO:
            problems.append(
                f"its last entry is not the last printed entry it named "
                f"(\"{(result.last_printed_entry_start or '')[:80]}\"), so the list may be cut short"
            )
    return problems


# The schema's entry types → BibTeX types `entry_from_fields` maps (a thesis has no closer type).
_BIB_TYPE = {"thesis": "misc", "report": "techreport"}


# A dash between page numbers, as typeset: the printed form of BibTeX's `--`.
_PAGE_DASH_RE = re.compile(r"(?<=\w)\s*[\u2010-\u2015\u2212]\s*(?=\w)")


def to_entries(result: PrintedReferenceList, *, read_by: str) -> list[BibEntry]:
    """The references as `BibEntry` records, keyed `r1`, `r2`, … in printed order.

    `raw_fields` carries the 1-based printed position (`pdf_ref_index`), the transcribed text
    (`pdf_raw`), the printed label, the model that read it (`pdf_read_by`) and its doubts, one per
    line (`pdf_doubts`) — reading notes, not findings. None of them enters the content hash, so the
    same reading by two models shares one verdict cache entry. A printed "et al." becomes the
    `others` marker a `.bib` uses, so the partial-author check knows the list is shortened on
    purpose, and a typeset dash in a page range becomes the `--` it was typeset from, so the range is
    not reported as a formatting nit the author never made.
    """
    entries: list[BibEntry] = []
    for index, ref in enumerate(result.references, start=1):
        key = f"r{index}"
        fields: dict[str, str] = {}
        for name, value in (
            ("title", ref.title), ("journal", ref.venue), ("publisher", ref.publisher),
            ("volume", ref.volume), ("number", ref.issue),
            ("pages", _PAGE_DASH_RE.sub("--", ref.pages) if ref.pages else None),
            ("doi", ref.doi), ("isbn", ref.isbn), ("url", ref.url),
            ("year", str(ref.year) if ref.year is not None else None),
        ):
            if value:
                fields[name] = value
        if ref.arxiv_id:
            fields["eprint"] = ref.arxiv_id
            fields["archiveprefix"] = "arXiv"
        fields["pdf_ref_index"] = str(index)
        fields["pdf_raw"] = ref.raw_text
        fields["pdf_read_by"] = read_by
        if ref.label:
            fields["pdf_label"] = ref.label
        if ref.doubts:
            fields["pdf_doubts"] = "\n".join(ref.doubts)
        authors = list(ref.authors) + (["others"] if ref.authors_truncated else [])
        entries.append(entry_from_fields(
            key, _BIB_TYPE.get(ref.entry_type, ref.entry_type), fields, authors=authors
        ))
    return entries


def pdf_text_layer(pdf_path: str | Path) -> str:
    """The PDF's whole text layer. Raises whatever pypdf raises; the caller reports it."""
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(Path(pdf_path)).pages)


def _squash(text: str) -> str:
    """Lower case without whitespace or hyphens of any kind. Text layers lose and mangle hyphens
    that the page image shows (`s41467 -024 50024 -6`), so a hyphen-only difference is never taken
    as absence."""
    return re.sub(r"[\s\-­‐-―−]+", "", text.lower())


def doi_problems(entries: list[BibEntry], text_layer: str | None) -> dict[str, list[str]]:
    """Per entry key, why its DOI cannot be trusted as read (entries with no problem are absent).

    * The DOI does not occur in the PDF's text layer, even ignoring whitespace and hyphens. Skipped
      when `text_layer` is None or nearly empty (a scanned PDF), which the caller reports.
    * Two entries with different titles carry the same DOI: one entry's tail spliced onto another,
      or a bibliography that prints it that way. Either way it is a finding, not a value to use.
    """
    problems: dict[str, list[str]] = {}
    squashed = _squash(text_layer) if text_layer and len(text_layer.strip()) > 200 else None
    if squashed is not None:
        for e in entries:
            if e.ids.doi and _squash(e.ids.doi) not in squashed:
                problems.setdefault(e.key, []).append(
                    f"the DOI {e.ids.doi} does not occur in the PDF's text"
                )
    by_doi: dict[str, list[BibEntry]] = {}
    for e in entries:
        if e.ids.doi:
            by_doi.setdefault(e.ids.doi, []).append(e)
    for doi, group in by_doi.items():
        if len(group) < 2 or all(title_ratio(group[0].title, o.title) >= 0.9 for o in group[1:]):
            continue
        refs = ", ".join(f"#{o.raw_fields.get('pdf_ref_index', o.key)}" for o in group)
        for e in group:
            problems.setdefault(e.key, []).append(
                f"the DOI {doi} is carried by references {refs}, which have different titles"
            )
    return problems


def counterpart(entry: BibEntry, others: list[BibEntry]) -> BibEntry | None:
    """The entry in another reading of the same list that transcribes the same printed reference.

    Compared on the transcribed text, with printed position breaking ties (a list can print the same
    reference twice). None when nothing is close enough to be the same printed text.
    """
    raw = _norm_text(entry.raw_fields.get("pdf_raw", ""))
    if not raw:
        return None
    position = int(entry.raw_fields.get("pdf_ref_index", "0") or 0)
    best: tuple[float, BibEntry] | None = None
    for other in others:
        score = fuzz.ratio(raw, _norm_text(other.raw_fields.get("pdf_raw", "")))
        score -= 0.01 * abs(position - int(other.raw_fields.get("pdf_ref_index", "0") or 0))
        if best is None or score > best[0]:
            best = (score, other)
    return best[1] if best is not None and best[0] >= 80 else None


def reading_differences(a: BibEntry, b: BibEntry) -> list[str]:
    """Each field two readings of one printed reference disagree on, with both values.

    Formatting a reader might vary without disagreeing (author name order "Smith, J." vs "J.
    Smith", spacing, case) is not a difference.
    """
    diffs: list[str] = []

    def differ(name: str, x: object, y: object) -> None:
        diffs.append(f"{name}: {a.raw_fields.get('pdf_read_by', 'first')} read {x!r}, "
                     f"{b.raw_fields.get('pdf_read_by', 'second')} read {y!r}")

    if (a.title or b.title) and title_ratio(a.title, b.title) < 0.99:
        differ("title", a.title, b.title)
    if len(a.authors) != len(b.authors) or not all(
        same_person(x, y) for x, y in zip(a.authors, b.authors, strict=False)
    ):
        differ("authors", "; ".join(a.authors), "; ".join(b.authors))
    if a.year != b.year:
        differ("year", a.year, b.year)
    if _norm_text(a.venue) != _norm_text(b.venue):
        differ("venue", a.venue, b.venue)
    if _norm_text(a.pages) != _norm_text(b.pages):
        differ("pages", a.pages, b.pages)
    for name in ("doi", "arxiv_id", "isbn13", "url"):
        x, y = getattr(a.ids, name), getattr(b.ids, name)
        if (x or "").lower() != (y or "").lower():
            differ(name, x, y)
    return diffs
