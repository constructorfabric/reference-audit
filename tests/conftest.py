"""Shared pytest fixtures.

Test papers live under ``tests/documents/<paper-title-slug>/<version>.{tex,bib}``,
where ``<version>`` names a state of the paper such as ``initial``, ``polished``,
``ideal-reference``, or ``deliberately-spoiled``. The ``initial`` version of the
pilot paper is the development oracle.
"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

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


# --- Live-dependency gating -----------------------------------------------------------------------
# The PDF extraction oracle needs a LaTeX toolchain and a running GROBID. Neither is required to run
# the rest of the suite, so by default those tests skip with a message naming the exact command that
# enables them. But an autodetect-and-skip gate would let a broken setup stay green forever, which is
# the silent-failure behavior AGENTS.md rules out — so with REFERENCE_AUDIT_LIVE=1 the same tests are
# required, and a missing dependency becomes a failure rather than a skip.

LIVE_ENV_VAR = "REFERENCE_AUDIT_LIVE"
GROBID_ENV_VAR = "GROBID_URL"
DEFAULT_GROBID_URL = "http://localhost:8070"

_ENABLE_HINT = (
    f"set {LIVE_ENV_VAR}=1 to require these tests, and start GROBID with:\n"
    "  podman run -d --name grobid -p 8070:8070 docker.io/grobid/grobid:0.8.2.1-crf"
)


def live_required() -> bool:
    return os.environ.get(LIVE_ENV_VAR, "") not in ("", "0", "false", "False")


def _unavailable(reason: str) -> None:
    """Skip when live tests are optional; fail when they were explicitly requested."""
    if live_required():
        pytest.fail(f"{LIVE_ENV_VAR}=1 requires this test, but {reason}")
    pytest.skip(f"{reason}. {_ENABLE_HINT}")


@pytest.fixture(scope="session")
def grobid_url() -> str:
    return os.environ.get(GROBID_ENV_VAR) or DEFAULT_GROBID_URL


@pytest.fixture
def require_latex() -> None:
    missing = [tool for tool in ("pdflatex", "xelatex", "bibtex") if shutil.which(tool) is None]
    if missing:
        _unavailable(f"the LaTeX toolchain is incomplete (not on PATH: {', '.join(missing)})")


@pytest.fixture
def require_grobid(grobid_url: str) -> None:
    try:
        with urlopen(f"{grobid_url}/api/isalive", timeout=5) as resp:  # noqa: S310 — fixed local URL
            if resp.status != 200:
                _unavailable(f"GROBID at {grobid_url} answered HTTP {resp.status}")
    except (OSError, ValueError) as exc:
        _unavailable(f"GROBID is not reachable at {grobid_url} ({type(exc).__name__})")
