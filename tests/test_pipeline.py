"""Parse-only report assembly (M1) over the real pilot."""

import pytest

from reference_audit.pipeline import EmptyBibliographyError, build_parse_report
from reference_audit.report import render_text


def _audit(report, key):
    for a in report.entries:
        if a.entry.key == key:
            return a
    raise KeyError(key)


def test_summary_counts(pilot_tex, pilot_bib):
    report = build_parse_report(pilot_tex, pilot_bib)
    s = report.summary
    assert s["total_entries"] == 28
    assert s["commented_twins"] == 1
    # The pilot's \input targets are all checked in (they are needed to compile it to PDF for the
    # extraction oracle), so citation coverage is complete: every entry is reached, nothing dangles.
    assert s["missing_includes"] == 0
    assert s["cited"] == 28
    assert s["uncited"] == 0
    assert report.cited_but_missing == []          # no dangling citations in the pilot
    assert "bagrov2024visual" in report.commented_twins


def test_wolpert_doi_issue(pilot_tex, pilot_bib):
    report = build_parse_report(pilot_tex, pilot_bib)
    issues = _audit(report, "wolpert2007").issues
    assert any("DOI normalized from URL form" in i for i in issues)


def test_book_isbn_issues(pilot_tex, pilot_bib):
    report = build_parse_report(pilot_tex, pilot_bib)
    for key in ("gavrilets2004", "mabook"):
        assert any("no ISBN" in i for i in _audit(report, key).issues)


def test_missing_doi_issues(pilot_tex, pilot_bib):
    report = build_parse_report(pilot_tex, pilot_bib)
    for key in ("plantec2023flow", "soros2014identifying"):
        assert any("no DOI/arXiv id" in i for i in _audit(report, key).issues)


def test_chan_cited_via_direct_and_nocite(pilot_tex, pilot_bib):
    report = build_parse_report(pilot_tex, pilot_bib)
    assert _audit(report, "chan2019lenia").entry.cited is True


def test_bib_only_has_no_uncited(pilot_bib):
    # without a .tex, nothing is 'uncited'
    report = build_parse_report(None, pilot_bib)
    assert report.uncited == []
    assert report.summary["total_entries"] == 28


def test_bib_only_declares_citedness_unknown(pilot_bib):
    """Without a manuscript, citedness is unknown, not "nothing is cited": the report says so rather
    than labelling every entry UNCITED."""
    report = build_parse_report(None, pilot_bib)
    assert report.citation_linking == "not_applicable"
    assert any("no manuscript (.tex)" in n for n in report.notes)
    text = render_text(report)
    assert "citedness unknown" in text
    assert "UNCITED" not in text
    assert "INPUT NOTES:" in text


def test_tex_and_bib_have_citedness_and_no_input_notes(pilot_tex, pilot_bib):
    report = build_parse_report(pilot_tex, pilot_bib)
    assert report.citation_linking == "available"
    assert report.notes == []


def test_empty_bib_raises(pilot_tex):
    # passing a non-.bib (e.g. swapped args, so the .tex is read as the bib) yields zero
    # auditable entries — this must raise, not silently report an all-zero audit.
    with pytest.raises(EmptyBibliographyError):
        build_parse_report(None, pilot_tex)
