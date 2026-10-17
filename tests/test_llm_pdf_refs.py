"""Offline checks of the LLM reference-extraction experiment harness (benchmarks/llm_pdf_refs.py)."""

import re

import llm_pdf_refs as L
import pytest
from openai.lib._pydantic import to_strict_json_schema


def test_v2_schema_is_strict_and_keeps_the_constraints():
    schema = to_strict_json_schema(L.ReferenceListV2)
    ref = schema["$defs"]["ExtractedReferenceV2"]
    assert ref["additionalProperties"] is False
    assert set(ref["required"]) == set(ref["properties"])
    # Every constrained field stays nullable: the escape hatch that keeps the model from emitting a
    # well-formed wrong value when what is printed does not fit.
    for field in ("doi", "arxiv_id", "isbn", "url"):
        branches = ref["properties"][field]["anyOf"]
        assert {"type": "null"} in branches
        assert any("pattern" in b for b in branches)
    year = ref["properties"]["year"]["anyOf"]
    assert {"type": "null"} in year
    assert any(b.get("minimum") == 1000 and b.get("maximum") == 2999 for b in year)
    # Output follows key order: the transcription comes first, the fields are read off it.
    assert next(iter(ref["properties"])) == "raw_text"


@pytest.mark.parametrize(
    ("pattern", "value", "ok"),
    [
        (L.DOI_PATTERN, "10.1207/S15327965PLI1104_01", True),
        (L.DOI_PATTERN, "10.1016/B978-155860643-2/50005-6", True),
        (L.DOI_PATTERN, "10.1207/S15327965PLI1104 01", False),
        (L.DOI_PATTERN, "https://doi.org/10.1038/nature14539", False),
        (L.DOI_PATTERN, "1O.1038/nature14539", False),
        (L.ARXIV_PATTERN, "2403.09549", True),
        (L.ARXIV_PATTERN, "1707.06347v2", True),
        (L.ARXIV_PATTERN, "hep-th/9901001", True),
        (L.ARXIV_PATTERN, "arXiv:2403.09549", False),
        (L.ISBN_PATTERN, "978-1-55860-643-2", True),
        (L.ISBN_PATTERN, "0-306-40615-X", True),
        (L.URL_PATTERN, "https://kaliningroup.github.io/mic_hackathon_2/", True),
        (L.URL_PATTERN, "https://example.org/a b", False),
    ],
)
def test_v2_patterns(pattern, value, ok):
    assert bool(re.fullmatch(pattern, value)) is ok


def test_v1_and_v2_instructions_differ_only_where_intended():
    assert "doubts" in L.INSTRUCTIONS_V2 and "doubts" not in L.INSTRUCTIONS
    assert L.INSTRUCTIONS_V2.startswith(L.INSTRUCTIONS.split("- doi:")[0])


def _ref(title, doi):
    return L.reference_to_entry("r", {"title": title, "doi": doi, "authors": ["A. B"],
                                      "raw_text": title, "entry_type": "article"})


def test_shared_dois_flags_one_doi_on_two_different_titles():
    kross = _ref("Facebook use predicts declines in subjective well-being", "10.1016/j.amepre.2017.01.010")
    primack = _ref("Social media use and perceived social isolation", "10.1016/j.amepre.2017.01.010")
    twin = _ref("Social media use and perceived social isolation", "10.1016/j.amepre.2017.01.010")
    assert L.shared_dois([kross, primack]) == ["10.1016/j.amepre.2017.01.010"]
    # A reference printed twice (same title) is a duplicate, not a spliced entry.
    assert L.shared_dois([primack, twin]) == []
    assert L.shared_dois([kross, _ref("Other", "10.1/x")]) == []
