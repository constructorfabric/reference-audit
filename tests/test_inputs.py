"""CLI positional-argument dispatch. Pure, no files touched."""

from __future__ import annotations

from pathlib import Path

import pytest

from reference_audit.inputs import (
    InvalidInputError,
    PdfInput,
    TexBibInput,
    default_cache_path,
    resolve_input,
)


def test_tex_plus_bib():
    got = resolve_input(Path("doc/main.tex"), Path("doc/refs.bib"))
    assert got == TexBibInput(bib_path=Path("doc/refs.bib"), tex_path=Path("doc/main.tex"))


def test_pdf_alone():
    assert resolve_input(Path("paper.pdf"), None) == PdfInput(Path("paper.pdf"))


def test_suffix_matching_is_case_insensitive():
    assert isinstance(resolve_input(Path("Paper.PDF"), None), PdfInput)
    assert isinstance(resolve_input(Path("Main.TeX"), Path("Refs.BIB")), TexBibInput)


def test_ltx_is_accepted_as_a_manuscript():
    """Only affects the swapped-argument diagnostic, but .ltx is a real LaTeX suffix."""
    with pytest.raises(InvalidInputError, match="swapped"):
        resolve_input(Path("refs.bib"), Path("main.ltx"))


def test_pdf_with_a_second_argument_is_rejected():
    with pytest.raises(InvalidInputError) as exc:
        resolve_input(Path("paper.pdf"), Path("refs.bib"))
    message = str(exc.value)
    assert "whole input" in message
    assert "audit paper.pdf" in message, "the message must name the command that works"


def test_pdf_as_the_second_argument_is_rejected():
    with pytest.raises(InvalidInputError) as exc:
        resolve_input(Path("main.tex"), Path("paper.pdf"))
    assert "audit paper.pdf" in str(exc.value)


def test_swapped_bib_and_tex_names_the_right_order():
    with pytest.raises(InvalidInputError) as exc:
        resolve_input(Path("refs.bib"), Path("main.tex"))
    assert "audit main.tex refs.bib" in str(exc.value)


def test_lone_bib_is_rejected_with_an_explanation():
    with pytest.raises(InvalidInputError) as exc:
        resolve_input(Path("refs.bib"), None)
    assert "stands alone" in str(exc.value)


def test_lone_unknown_suffix_is_rejected():
    with pytest.raises(InvalidInputError, match="expected"):
        resolve_input(Path("notes.txt"), None)


def test_second_argument_must_be_a_bib():
    with pytest.raises(InvalidInputError) as exc:
        resolve_input(Path("main.tex"), Path("refs.txt"))
    assert "must be the .bib" in str(exc.value)


def test_cache_path_is_anchored_on_the_bib_for_a_pair():
    source = resolve_input(Path("doc/main.tex"), Path("other/refs.bib"))
    assert default_cache_path(source) == Path("other/.reference_audit/cache.db")


def test_cache_path_is_anchored_on_the_pdf():
    source = resolve_input(Path("papers/paper.pdf"), None)
    assert default_cache_path(source) == Path("papers/.reference_audit/cache.db")
