r"""BibTeX records that a PDF carries for its own references.

A conference template can make the PDF carry the authored records of its cited works, so that they
are read rather than re-extracted from the typeset text. The AI4X template does: its bibliography
style (`ai4x.bst`) writes each cited entry's BibTeX record into the `.bbl`, and its class embeds them
as the PDF attachment `cited-references.bib.hex`:

* only the cited entries (BibTeX sees nothing else), in the printed order;
* only the fields printed in the reference list plus identifiers (DOI, ISBN, arXiv id, …), with
  `@string` macros and `crossref` already resolved by BibTeX;
* hex-encoded, because the records pass through TeX on the way to the attachment and a raw `%`, `#`
  or `\` in a field would break it. Whitespace is insignificant: BibTeX and TeX wrap the lines.

The records are what the author compiled, not necessarily what is printed — a hand-edited `.bbl`
can make them differ — so `title_printed` compares each one with the PDF's reference list.

Local file I/O only: nothing here touches the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from anyascii import anyascii
from pypdf import PdfReader

ATTACHMENT_NAME = "cited-references.bib.hex"

# The last heading of the reference list; the text after it is what the titles are looked up in.
_REFERENCES_HEADING_RE = re.compile(r"(?im)^\s*(?:references|bibliography)\s*$")
# What breaks a title into separately-checked segments: math, LaTeX commands, escaped characters.
_TITLE_BREAK_RE = re.compile(r"\$[^$]*\$|\\[a-zA-Z]+\*?|\\.")
# A title segment shorter than this is too unspecific to look up.
_MIN_SEGMENT_LETTERS = 4


@dataclass(frozen=True)
class EmbeddedReferences:
    """The outcome of looking for the attachment.

    `bib_text` is the decoded `.bib`, or None. `problem` says why there is none when that is not the
    ordinary case of a PDF without the attachment: the PDF could not be read, or the attachment is
    malformed. Either way the caller falls back to extracting the references, and reports `problem`.
    """

    bib_text: str | None = None
    problem: str | None = None


def read_embedded_references(pdf_path: str | Path) -> EmbeddedReferences:
    """Find and decode the `cited-references.bib.hex` attachment of a PDF."""
    path = Path(pdf_path)
    try:
        attachments = PdfReader(path).attachments
        payloads = attachments.get(ATTACHMENT_NAME, [])
    except Exception as exc:  # noqa: BLE001 — pypdf raises many types for a damaged PDF
        return EmbeddedReferences(
            problem=f"could not look for embedded BibTeX records in {path.name}: "
            f"{type(exc).__name__}: {exc}"
        )
    if not payloads:
        return EmbeddedReferences()
    if len(payloads) > 1:
        return EmbeddedReferences(
            problem=f"{path.name} embeds {len(payloads)} attachments named {ATTACHMENT_NAME}, so it "
            "is ambiguous which holds the references"
        )
    try:
        return EmbeddedReferences(bib_text=decode_hex_payload(payloads[0]))
    except ValueError as exc:
        return EmbeddedReferences(problem=f"the {ATTACHMENT_NAME} attachment of {path.name} {exc}")


def decode_hex_payload(payload: bytes) -> str:
    """Hex with arbitrary whitespace → text. Raises ValueError (worded as a predicate) if malformed."""
    digits = re.sub(rb"\s+", b"", payload)
    if not digits:
        raise ValueError("is empty")
    if re.fullmatch(rb"[0-9a-fA-F]+", digits) is None:
        raise ValueError("is not hex-encoded")
    if len(digits) % 2:
        raise ValueError("has an odd number of hex digits (truncated)")
    # A .bib in another encoding still parses; the replaced bytes are what `parse_bib` would get too.
    return bytes.fromhex(digits.decode("ascii")).decode("utf-8", errors="replace")


def reference_list_text(pdf_path: str | Path) -> str:
    """The PDF's text after its last References/Bibliography heading (all of it, if there is none).

    Raises whatever pypdf raises when the text cannot be extracted; the caller reports it.
    """
    reader = PdfReader(Path(pdf_path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    headings = list(_REFERENCES_HEADING_RE.finditer(text))
    return text[headings[-1].end():] if headings else text


def _letters(text: str) -> str:
    """Lower-case ASCII letters only: hyphenation, line breaks, ligatures and accents fall away."""
    return re.sub(r"[^a-z]", "", anyascii(text).lower())


def title_printed(title: str, printed_text: str) -> bool | None:
    """Whether an embedded record's title appears in the printed reference list.

    The title is split at math and LaTeX commands, whose typeset form differs from the source, and
    every remaining segment must appear in the printed text. None when no segment is long enough to
    be looked up — a title like "$\\alpha$" cannot be checked this way.
    """
    printed = _letters(printed_text)
    segments = [s for s in map(_letters, _TITLE_BREAK_RE.split(title))
                if len(s) >= _MIN_SEGMENT_LETTERS]
    if not segments:
        return None
    return all(s in printed for s in segments)
