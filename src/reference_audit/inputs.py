"""What the positional CLI arguments mean. Pure and side-effect-free, so it is directly testable.

Two input shapes are supported, distinguished by file extension:

    reference-audit audit main.tex references.bib     # authored sources
    reference-audit audit paper.pdf                   # GROBID extracts both from the PDF

Every rejected combination gets a message naming the fix, because "usage: ..." tells a user what the
grammar is but not which of their two paths was the wrong one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CACHE_DIRNAME = ".reference_audit"
_TEX_SUFFIXES = (".tex", ".ltx")


class InvalidInputError(ValueError):
    """The given paths are not a recognized input combination."""


@dataclass(frozen=True)
class TexBibInput:
    """A `.bib` to audit, optionally with the manuscript that cites it."""

    bib_path: Path
    tex_path: Path | None = None

    @property
    def anchor(self) -> Path:
        return self.bib_path


@dataclass(frozen=True)
class PdfInput:
    """A PDF that is the whole input: its references and citations are both extracted from it."""

    pdf_path: Path

    @property
    def anchor(self) -> Path:
        return self.pdf_path


AuditInput = TexBibInput | PdfInput


def resolve_input(first: Path, second: Path | None) -> AuditInput:
    """Classify the positional arguments, or explain why they cannot be."""
    a = first.suffix.lower()
    b = second.suffix.lower() if second is not None else None

    if a == ".pdf":
        if second is not None:
            raise InvalidInputError(
                f"a PDF is the whole input: GROBID extracts both the reference list and the in-text "
                f"citations from {first.name}, so there is nothing for {second.name} to add. "
                f"Run 'reference-audit audit {first.name}' on its own, or pass a .tex + .bib pair "
                f"instead."
            )
        return PdfInput(first)

    if b == ".pdf":
        raise InvalidInputError(
            f"a .pdf cannot be combined with another input ({first.name} + {second.name}). "
            f"Run 'reference-audit audit {second.name}' on its own."
        )

    if second is None:
        raise InvalidInputError(
            f"expected 'audit <paper.pdf>' or 'audit <main.tex> <references.bib>', but got a single "
            f"{a or 'extension-less'} file ({first.name}). A .bib needs its manuscript; a PDF stands "
            f"alone."
        )

    if a == ".bib" and b in _TEX_SUFFIXES:
        raise InvalidInputError(
            f"the arguments look swapped — the manuscript comes first: "
            f"'reference-audit audit {second.name} {first.name}'."
        )

    if b != ".bib":
        raise InvalidInputError(
            f"the second argument must be the .bib bibliography to audit, but {second.name} has "
            f"suffix '{b or '(none)'}'."
        )

    return TexBibInput(bib_path=second, tex_path=first)


def default_cache_path(source: AuditInput) -> Path:
    """`<input_dir>/.reference_audit/cache.db`, beside whichever file the run is anchored on."""
    return source.anchor.parent / CACHE_DIRNAME / "cache.db"
