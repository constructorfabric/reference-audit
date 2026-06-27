"""Cross-document coverage.

A smoke test runs the parse pipeline over every on-disk paper version, and a
pinned regression test locks the summary counts for the position paper's two
versions (`initial` from commit a3802eb, `polished` the current working tree).
"""

import pytest

from reference_audit.parsing.bib import parse_bib
from reference_audit.pipeline import build_parse_report

from conftest import discover_document_versions

DOC_VERSIONS = discover_document_versions()


@pytest.mark.parametrize("doc", DOC_VERSIONS, ids=[d.id for d in DOC_VERSIONS])
def test_every_version_parses(doc):
    """Each version's .tex/.bib parse and assemble into a coherent report."""
    entries, _ = parse_bib(doc.bib)
    assert entries, f"{doc.id}: no bib entries parsed"

    report = build_parse_report(doc.tex, doc.bib)
    s = report.summary
    assert s["total_entries"] == len(entries)
    assert s["cited"] + s["uncited"] == s["total_entries"]
    # every cited key resolves to a known entry
    assert report.cited_but_missing == []


POSITION_SLUG = "position-align-ai-to-aspirations"


@pytest.mark.parametrize(
    "version, total, cited, uncited, with_issues",
    [
        ("initial", 120, 115, 5, 45),
        ("polished", 144, 134, 10, 31),
    ],
)
def test_position_summary_counts(version, total, cited, uncited, with_issues):
    report = build_parse_report(
        f"tests/documents/{POSITION_SLUG}/{version}.tex",
        f"tests/documents/{POSITION_SLUG}/{version}.bib",
    )
    s = report.summary
    assert s["total_entries"] == total
    assert s["cited"] == cited
    assert s["uncited"] == uncited
    assert s["entries_with_issues"] == with_issues
