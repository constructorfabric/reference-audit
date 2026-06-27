"""Shared pytest fixtures.

Test papers live under ``tests/documents/<paper-title-slug>/<version>.{tex,bib}``,
where ``<version>`` names a state of the paper such as ``initial``, ``polished``,
``ideal-reference``, or ``deliberately-spoiled``. The ``initial`` version of the
pilot paper is the development oracle.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
DOCUMENTS_DIR = TESTS_DIR / "documents"

PILOT_SLUG = "directing-open-ended-evolution"
PILOT_VERSION = "initial"


def document_path(slug: str, version: str, suffix: str) -> Path:
    return DOCUMENTS_DIR / slug / f"{version}{suffix}"


@dataclass(frozen=True)
class DocumentVersion:
    """A single (slug, version) test paper with its .tex and .bib paths."""

    slug: str
    version: str

    @property
    def id(self) -> str:
        return f"{self.slug}/{self.version}"

    @property
    def tex(self) -> Path:
        return document_path(self.slug, self.version, ".tex")

    @property
    def bib(self) -> Path:
        return document_path(self.slug, self.version, ".bib")


def discover_document_versions() -> list[DocumentVersion]:
    """All on-disk versions that have both a .tex and a .bib, sorted by id."""
    found = []
    for slug_dir in sorted(DOCUMENTS_DIR.iterdir()):
        if not slug_dir.is_dir():
            continue
        for tex in sorted(slug_dir.glob("*.tex")):
            if tex.with_suffix(".bib").exists():
                found.append(DocumentVersion(slug_dir.name, tex.stem))
    return found


@pytest.fixture
def pilot_bib() -> Path:
    return document_path(PILOT_SLUG, PILOT_VERSION, ".bib")


@pytest.fixture
def pilot_tex() -> Path:
    return document_path(PILOT_SLUG, PILOT_VERSION, ".tex")
