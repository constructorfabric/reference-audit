"""The pure side of reading a PDF's references with an LLM (`pdf.llm_refs`). No network, no PDF."""

from __future__ import annotations

import re

import pytest
from openai.lib._pydantic import to_strict_json_schema

from reference_audit.pdf.llm_refs import (
    ARXIV_PATTERN,
    DOI_PATTERN,
    ISBN_PATTERN,
    URL_PATTERN,
    PrintedReference,
    PrintedReferenceList,
    counterpart,
    doi_problems,
    list_problems,
    reading_differences,
    to_entries,
)


def ref(title: str | None, authors=("A. Author",), *, year: int | None = 2020, doi=None,
        raw: str | None = None, venue="J. Things", truncated=False, arxiv=None, doubts=()):
    return PrintedReference(
        raw_text=raw or f"{', '.join(authors)}. {title}. {venue}, {year}.",
        label=None, entry_type="article", title=title, authors=list(authors),
        authors_truncated=truncated, year=year, venue=venue, publisher=None, volume=None,
        issue=None, pages=None, doi=doi, arxiv_id=arxiv, isbn=None, url=None, doubts=list(doubts),
    )


def reading(*refs: PrintedReference, coverage="all_entries", anchor: str | None = None,
            problems=()) -> PrintedReferenceList:
    return PrintedReferenceList(
        has_reference_list=bool(refs),
        last_printed_entry_start=anchor if anchor is not None
        else (refs[-1].raw_text[:60] if refs else None),
        references=list(refs), coverage=coverage, problems=list(problems),
    )


A = ref("Scaling laws for neural language models", ("J. Kaplan", "S. McCandlish"))
B = ref("Attention is all you need", ("A. Vaswani",), year=2017)
C = ref("Deep residual learning for image recognition", ("K. He",), year=2016)


# ------------------------------------------------------------------------------- schema


def test_schema_is_strict_and_orders_the_completeness_fields_around_the_list():
    schema = to_strict_json_schema(PrintedReferenceList)
    keys = list(schema["properties"])
    # The last entry is named before transcribing, coverage is reported after: a list that stops
    # early then disagrees with its own anchor.
    assert keys.index("last_printed_entry_start") < keys.index("references") < keys.index("coverage")
    entry = schema["$defs"]["PrintedReference"]
    assert entry["additionalProperties"] is False and set(entry["required"]) == set(entry["properties"])
    assert next(iter(entry["properties"])) == "raw_text"
    for field in ("doi", "arxiv_id", "isbn", "url"):
        branches = entry["properties"][field]["anyOf"]
        assert {"type": "null"} in branches, f"{field} must keep its null escape hatch"
        assert any("pattern" in b for b in branches)


@pytest.mark.parametrize(
    ("pattern", "value", "ok"),
    [
        (DOI_PATTERN, "10.1207/S15327965PLI1104_01", True),
        (DOI_PATTERN, "10.1207/S15327965PLI1104 01", False),
        (DOI_PATTERN, "https://doi.org/10.1038/nature14539", False),
        (DOI_PATTERN, "1O.1038/nature14539", False),
        (ARXIV_PATTERN, "2403.09549v2", True),
        (ARXIV_PATTERN, "hep-th/9901001", True),
        (ARXIV_PATTERN, "arXiv:2403.09549", False),
        (ISBN_PATTERN, "978-1-55860-643-2", True),
        (URL_PATTERN, "https://example.org/a b", False),
    ],
)
def test_patterns(pattern, value, ok):
    assert bool(re.fullmatch(pattern, value)) is ok


# ------------------------------------------------------------------------------- list checks


def test_a_complete_consistent_list_has_no_problems():
    assert list_problems(reading(A, B, C)) == []


def test_a_document_without_a_reference_list_is_consistent():
    assert list_problems(reading()) == []


def test_a_list_that_stops_before_its_named_last_entry_is_rejected():
    # The failure that motivated the anchor: 30 of 134 entries, a completed response, schema-valid.
    truncated = reading(A, B, anchor=C.raw_text[:60])
    (problem,) = list_problems(truncated)
    assert "not the last printed entry it named" in problem


def test_a_list_that_reports_stopping_early_is_rejected_with_its_own_words():
    problems = list_problems(reading(A, B, coverage="stopped_early", problems=["ran out"]))
    assert any("stopped before the end" in p and "ran out" in p for p in problems)


def test_a_printed_label_on_the_anchor_does_not_matter():
    labelled = reading(A, B, anchor="[2] " + B.raw_text[:50])
    assert list_problems(labelled) == []


@pytest.mark.parametrize(
    ("result", "fragment"),
    [
        (PrintedReferenceList(has_reference_list=False, last_printed_entry_start=None,
                              references=[A], coverage="all_entries", problems=[]),
         "no reference list but returned 1"),
        (PrintedReferenceList(has_reference_list=True, last_printed_entry_start="x",
                              references=[], coverage="all_entries", problems=[]),
         "returned no entries"),
        (reading(A, anchor=""), "did not name the last printed entry"),
    ],
)
def test_inconsistent_lists_are_rejected(result, fragment):
    assert any(fragment in p for p in list_problems(result))


# ------------------------------------------------------------------------------- entries


def test_entries_keep_printed_order_provenance_and_doubts():
    doubting = ref("A title", doi="10.1234/x", doubts=["line break inside the DOI"])
    entries = to_entries(
        reading(A, ref("Shortened", ("A. B",), truncated=True, arxiv="2401.01234"), doubting),
        read_by="model-x",
    )
    assert [e.key for e in entries] == ["r1", "r2", "r3"]
    assert [e.raw_fields["pdf_ref_index"] for e in entries] == ["1", "2", "3"]
    assert all(e.raw_fields["pdf_read_by"] == "model-x" for e in entries)
    assert entries[0].raw_fields["pdf_raw"] == A.raw_text
    # A printed "et al." is the marker a .bib uses, so the partial-author check knows.
    assert entries[1].authors == ["A. B", "others"]
    assert entries[1].ids.arxiv_id == "2401.01234"
    # Doubts are reading notes kept with the entry, not issues.
    assert entries[2].raw_fields["pdf_doubts"] == "line break inside the DOI"
    assert "pdf_doubts" not in entries[0].raw_fields


def test_a_typeset_page_dash_is_the_bibtex_double_hyphen_it_was_typeset_from():
    printed = ref("T").model_copy(update={"pages": "2769–2782"})
    (entry,) = to_entries(reading(printed), read_by="m")
    assert entry.pages == "2769--2782"


def test_the_same_reading_by_two_models_shares_a_content_hash():
    (by_first,) = to_entries(reading(A), read_by="first")
    (by_second,) = to_entries(reading(A), read_by="second")
    assert by_first.content_hash == by_second.content_hash


# ------------------------------------------------------------------------------- DOI checks

TEXT = ("References\n[1] J. Kaplan, S. McCandlish. Scaling laws for neural language models. "
        "doi: 10.1038/s41467 -024 50024 -6.\n[2] A. Vaswani. Attention is all you need. "
        "doi:10.5555/3295222.3295349\n" + "filler text " * 20)


def test_a_doi_missing_from_the_text_layer_is_flagged_but_a_hyphen_lost_by_it_is_not():
    entries = to_entries(reading(
        ref("Scaling laws for neural language models", doi="10.1038/s41467-024-50024-6"),
        ref("Attention is all you need", doi="10.1016/j.amepre.2013.01.006"),
    ), read_by="m")
    problems = doi_problems(entries, TEXT)
    assert list(problems) == ["r2"]
    assert "does not occur in the PDF's text" in problems["r2"][0]


def test_without_a_usable_text_layer_dois_are_not_checked_against_it():
    entries = to_entries(reading(ref("X", doi="10.1234/nowhere")), read_by="m")
    assert doi_problems(entries, None) == {}
    assert doi_problems(entries, "   ") == {}


def test_one_doi_on_two_different_titles_flags_both_but_a_printed_twin_does_not():
    spliced = to_entries(reading(
        ref("Facebook use predicts declines in subjective well-being", doi="10.1016/j.amepre.2017.01.010"),
        ref("Social media use and perceived social isolation", doi="10.1016/j.amepre.2017.01.010"),
    ), read_by="m")
    problems = doi_problems(spliced, None)
    assert set(problems) == {"r1", "r2"} and "references #1, #2" in problems["r1"][0]
    twins = to_entries(reading(
        ref("Social media use and perceived social isolation", doi="10.1016/j.amepre.2017.01.010"),
        ref("Social media use and perceived social isolation", doi="10.1016/j.amepre.2017.01.010"),
    ), read_by="m")
    assert doi_problems(twins, None) == {}


# ------------------------------------------------------------------------------- two readings


def test_counterpart_finds_the_same_printed_reference_and_breaks_ties_by_position():
    first = to_entries(reading(A, B, B, C), read_by="first")
    second = to_entries(reading(A, B, B, C), read_by="second")
    assert counterpart(first[2], second).key == "r3"
    assert counterpart(first[0], second).key == "r1"
    stranger = to_entries(reading(ref("Something else entirely", ("Z. Zed",), venue="Q")),
                             read_by="first")
    assert counterpart(stranger[0], second) is None


def test_name_order_is_not_a_reading_difference_but_a_doi_is():
    (a,) = to_entries(reading(ref("T", ("Smith, J.",), doi="10.1234/abc")), read_by="first")
    (b,) = to_entries(reading(ref("T", ("J. Smith",), doi="10.1234/abc")), read_by="second")
    assert reading_differences(a, b) == []
    (c,) = to_entries(reading(ref("T", ("J. Smith",), doi="10.1234/abd")), read_by="second")
    (diff,) = reading_differences(a, c)
    assert diff == "doi: first read '10.1234/abc', second read '10.1234/abd'"
