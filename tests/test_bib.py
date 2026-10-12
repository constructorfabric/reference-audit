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


def _parse(raw):
    from reference_audit.parsing.bib import parse_bib_text

    return parse_bib_text(raw)


def test_string_references_concatenation_and_month_macros_are_resolved():
    entries, _, unparsed = _parse(
        '@string{conf = "Proc. of " # {ICML}}\n'
        '@string{ICMLYEAR = {2020}}\n'
        '@inproceedings{a, title={T}, booktitle = conf # " " # icmlyear, month = jul, year = 2020}\n'
    )
    assert unparsed == []
    (a,) = entries
    assert a.venue == "Proc. of ICML 2020"
    assert a.raw_fields["month"] == "July"
    assert a.year == 2020


def test_an_undefined_string_makes_only_its_entry_unparsed():
    entries, _, unparsed = _parse(
        "@article{a, title={One}, journal = nosuch}\n@article{b, title={Two}}\n"
    )
    assert [e.key for e in entries] == ["b"]
    (u,) = unparsed
    assert (u.key, u.line) == ("a", 1)
    assert "field `journal`: undefined @string `nosuch`" in u.reason
    assert "not checked" in u.reason


def test_a_duplicate_field_is_not_resolved_by_picking_one():
    entries, _, unparsed = _parse(
        "@article{a, title={One}, Title={Two}}\n@article{b, title={X}, title={Y}}\n"
    )
    assert entries == []
    assert [(u.key, u.reason.split(";")[0]) for u in unparsed] == [
        ("a", "duplicate field `title`"),
        ("b", "duplicate field `title`"),
    ]


def test_a_repeated_key_audits_both_entries():
    entries, _, unparsed = _parse("@article{a, title={One}}\n@article{a, title={Two}}\n")
    assert [(e.key, e.title) for e in entries] == [("a", "One"), ("a", "Two")]
    assert unparsed == []


def test_a_balanced_but_malformed_entry_carries_the_parser_reason():
    entries, _, unparsed = _parse("@article{k title={X}}\n@article{ok, title={Y}}\n")
    assert [e.key for e in entries] == ["ok"]
    (u,) = unparsed
    assert u.key == "k"
    assert "Expected comma after entry key" in u.reason


def test_bibtex_literals_survive_latex_decoding():
    (e,), _, _ = _parse(
        "@article{a, title = {Allocation &amp; {GAN}s for d&apos;Amore},"
        " author = {M{\\\"u}ller, J. and Garc{\\'\\i}a, M.},"
        " pages = {65--92},"
        " url = {https://x.org/~u/a%20b?q=1#f},"
        " note = {\\url{https://y.org} and \\href{https://z.org}{Zed}}}\n"
    )
    assert e.title == "Allocation & GANs for d'Amore"
    assert e.authors == ["Müller, J.", "García, M."]
    assert e.pages == "65--92"
    assert e.ids.url == "https://x.org/~u/a%20b?q=1#f"
    assert e.raw_fields["note"] == "https://y.org and Zed <https://z.org>"


def test_commented_twins_are_read_from_the_uncommented_text():
    entries, twins, unparsed = _parse(
        "@misc{x, title={x}}\n"
        "%@misc{full,\n%  title={Commented {\\'E}cole},\n%  year={2001}\n%}\n"
        "%@misc{single, title={Single line}}\n"
        "@misc{w, title={w}}\n"
    )
    assert [e.key for e in entries] == ["x", "w"]
    assert [(t.key, t.title, t.is_commented) for t in twins] == [
        ("full", "Commented École", True),
        ("single", "Single line", True),
    ]
    assert twins[0].year == 2001
    assert unparsed == []


def test_an_entry_without_fields_is_unparsed():
    entries, _, unparsed = _parse("@article{key}\n@article{ok, title={x}}\n")
    assert [e.key for e in entries] == ["ok"]
    (u,) = unparsed
    assert u.key == "key"
    assert u.reason.startswith("the entry has no fields")
