"""Shared pytest fixtures. The pilot .bib/.tex are the development oracle."""

from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent


@pytest.fixture
def pilot_bib() -> Path:
    return TESTS_DIR / "references.bib"


@pytest.fixture
def pilot_tex() -> Path:
    return TESTS_DIR / "main.tex"
