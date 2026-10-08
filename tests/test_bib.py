"""BibTeX parsing on the real pilot — entry count, the commented twin, and T3 quirks."""

from reference_audit.models import EntryType
from reference_audit.parsing.bib import parse_bib


def _by_key(entries):
    return {e.key: e for e in entries}


def test_pilot_entry_count_and_twin(pilot_bib):
    entries, twins, _ = parse_bib(pilot_bib)
    keys = {e.key for e in entries}
    # 28 real entries; the commented %@misc{bagrov2024visual} is a twin, not audited
    assert len(entries) == 28
    assert "bagrov2024visual" not in keys
    assert [t.key for t in twins] == ["bagrov2024visual"]


def test_commented_twin_is_kravchenko_preprint(pilot_bib):
    _, twins, _ = parse_bib(pilot_bib)
    twin = twins[0]
    assert twin.is_commented is True
    assert twin.ids.arxiv_id == "2408.04076"
    # the malformed "and and" must not leave an empty author
    assert "" not in twin.authors
    assert len(twin.authors) == 4
    assert any("Kravchenko" in a for a in twin.authors)


def test_wolpert_doi_normalized(pilot_bib):
    entries, _, _ = parse_bib(pilot_bib)
    wolpert = _by_key(entries)["wolpert2007"]
    assert wolpert.ids.doi == "10.1002/cplx.20165"  # https:// prefix stripped


def test_kravchenko_published(pilot_bib):
    entries, _, _ = parse_bib(pilot_bib)
    k = _by_key(entries)["kravchenko2026"]
    assert k.ids.doi == "10.1177/03010066251384492"
    assert k.year == 2026
    assert k.venue == "Perception"


def test_pugh_has_no_spurious_arxiv(pilot_bib):
    entries, _, _ = parse_bib(pilot_bib)
    pugh = _by_key(entries)["pugh2016quality"]
    assert pugh.ids.arxiv_id is None
    assert pugh.ids.doi == "10.3389/frobt.2016.00040"


def test_kumar_arxiv_misc(pilot_bib):
    entries, _, _ = parse_bib(pilot_bib)
    kumar = _by_key(entries)["kumar2024automating"]
    assert kumar.entry_type == EntryType.MISC
    assert kumar.ids.arxiv_id == "2412.17799"


def test_books_without_isbn(pilot_bib):
    entries, _, _ = parse_bib(pilot_bib)
    by = _by_key(entries)
    for key in ("gavrilets2004", "mabook"):
        assert by[key].entry_type == EntryType.BOOK
        assert by[key].ids.isbn13 is None


def test_latex_accent_decoded(pilot_bib):
    entries, _, _ = parse_bib(pilot_bib)
    plantec = _by_key(entries)["plantec2023flow"]
    # Cl{\'e}ment -> Clément
    assert any("Clément" in a for a in plantec.authors)


_BROKEN_BIB = r"""@article{first, title={First}, author={Doe, J.}, year={2020}}

@article{bad,
  title = {Broken},
  author = {Man{\'e, R. and Smith, J.},
  year = {2021}
}

@string{venue = "ICML"}
@comment{not an entry}
%@misc{twin, title={A commented twin}}

@article{third, title={Third}, author={Roe, K.}, year={2022}}
"""


def test_an_entry_the_parser_drops_is_reported_with_its_reason(tmp_path):
    bib = tmp_path / "r.bib"
    bib.write_text(_BROKEN_BIB, encoding="utf-8")
    entries, twins, unparsed = parse_bib(bib)
    assert [e.key for e in entries] == ["first", "third"]  # the neighbours survive
    assert [t.key for t in twins] == ["twin"]               # a twin is not "unparsed"
    (u,) = unparsed                                         # nor are @string/@comment
    assert (u.key, u.entry_type, u.line) == ("bad", "article", 3)
    assert "unbalanced braces (1 more '{' than '}')" in u.reason
    assert "not checked" in u.reason


def test_the_report_lists_unparsed_entries_and_does_not_call_them_missing(tmp_path):
    from reference_audit.pipeline import build_parse_report
    from reference_audit.report import render_text

    bib = tmp_path / "r.bib"
    bib.write_text(_BROKEN_BIB, encoding="utf-8")
    tex = tmp_path / "m.tex"
    tex.write_text(r"\cite{first,bad,third,ghost}", encoding="utf-8")
    report = build_parse_report(tex, bib)
    assert [u.key for u in report.unparsed] == ["bad"]
    assert report.cited_but_missing == ["ghost"]  # `bad` exists, it just could not be read
    assert report.summary["unparsed"] == 1
    text = render_text(report)
    assert "UNPARSEABLE .bib ENTRIES (1) — could not be read, so not checked:" in text
    assert "[article] bad  (line 3)" in text
    assert "1 unparseable" in text


def test_a_bib_whose_only_entry_is_unparseable_names_it(tmp_path):
    import pytest

    from reference_audit.pipeline import EmptyBibliographyError, build_parse_report

    bib = tmp_path / "r.bib"
    bib.write_text("@article{bad, title={Broken, year={2021}}\n", encoding="utf-8")
    with pytest.raises(EmptyBibliographyError, match=r"1 unparseable entry: bad \(line 1\)"):
        build_parse_report(None, bib)
