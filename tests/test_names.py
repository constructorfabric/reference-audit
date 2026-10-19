"""Person-level author matching (`matching/names.py`): the author check and the pooling guard.

Cases are from the HALLMARK dev_public run (pipeline 0.21), where a surname-only fuzzy match made
real authors look fabricated and let a fabricated one pass.
"""

import pytest
from typer.testing import CliRunner

from reference_audit import cli
from reference_audit.cli import app
from reference_audit.matching.names import (
    authors_compatible,
    check_cited_authors,
    omitted_authors,
    same_person,
)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Tian Li", "Li Tian"),                                 # OpenAlex swaps given and family
        ("Pablo Moriano", "Moriano Pablo"),
        ("Lierni Sestorain", "Sestorain Saralegui, Lierni"),    # a second surname
        ("Carlos Riquelme Ruiz", "Carlos Riquelme"),
        ("Norberto Fernández García", "Norberto Fernandez"),
        ("Raphael Gontijo-Lopes", "Raphael Gontijo Lopes"),     # hyphen vs space
        ("Guo-Sen Xie", "Guosen Xie"),                          # hyphen vs none
        ("Hung Tran-The", "Hung The Tran"),
        ("Thommen George Karimpanal", "George, Thommen Karimpanal"),
        ("D.P. Woodruff", "David P. Woodruff"),                 # initials
        ("Ed H. Chi", "Ed H."),                                 # a source that dropped the surname
        ("Hans Müller", "Hans Mueller"),                        # umlaut transliteration
        ("Jure Zbontar", "Jure Žbontar"),
        ("Francesco d&apos;Amore", "Francesco d'Amore"),        # an HTML entity
        ("A. B.", "Alice Brown"),                               # initials only
        ("Tim Mann", "Timothy A. Mann"),                        # a diminutive
        ("William Fedus", "Liam Fedus"),
        ("Aleksandar Spiridonov", "Alexander Spiridonov"),      # a transliteration
    ],
)
def test_same_person(a, b):
    assert same_person(a, b)
    assert same_person(b, a)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("João Carreira", "Ricardo Barreira"),   # 0.875 surname similarity passed the old check
        ("Wenhan Yang", "Wenhan Wang"),
        ("J. Smith", "Adam Smith"),              # the initial contradicts the given name
        ("Lu Yao", "Wei Chen"),
        ("Wei Chen", "Wei Cheng"),                # no prefix rule: Chen / Cheng are two surnames
        ("Peter Schuh", "Parker Schuh"),
    ],
)
def test_different_people(a, b):
    assert not same_person(a, b)


def test_a_record_with_other_authors_is_checked_in_full():
    missing, unchecked = check_cited_authors(
        ["Zhao Song", "Jian Tang", "Hao Yan"], [["Yiting Cheng", "Fangyun Wei"]]
    )
    assert (missing, unchecked) == (["Zhao Song", "Jian Tang", "Hao Yan"], [])


def test_and_others_is_an_explicit_truncation():
    full = ["Stephen Casper", "Xander Davies", "Claudia Shi"]
    assert omitted_authors(["Casper, Stephen", "others"], full) == []
    assert omitted_authors(["Casper, Stephen"], full) == ["Xander Davies", "Claudia Shi"]


def test_a_journal_extension_with_other_authors_is_not_compatible():
    cf = ["Wenxiao Wang", "Lu Yao", "Long Chen", "Binbin Lin", "Deng Cai", "Xiaofei He", "Wei Liu"]
    cfpp = ["Wenxiao Wang", "Wei Chen", "Qibo Qiu", "Long Chen", "Boxi Wu", "Binbin Lin",
            "Xiaofei He", "Wei Liu"]
    assert not authors_compatible(cf, cfpp)
    assert authors_compatible(cf, [*cf, "Added Author"])   # a version that adds an author
    assert authors_compatible(cf, [])                       # unknown is not a conflict


def test_cli_rejects_an_unknown_partial_authors_mode(tmp_path):
    tex, bib = tmp_path / "p.tex", tmp_path / "p.bib"
    tex.write_text("\\cite{k}", encoding="utf-8")
    bib.write_text("@article{k, title={T}}", encoding="utf-8")
    result = CliRunner().invoke(app, ["audit", str(tex), str(bib), "--partial-authors", "maybe"])
    assert result.exit_code != 0
    assert "--partial-authors must be" in result.output


def test_cli_rejects_an_unknown_fail_on_verdict_before_the_audit_runs(tmp_path, monkeypatch):
    # A typo in --fail-on must not cost a full audit run before it is reported (issue #5).
    def no_audit(*args, **kwargs):
        raise AssertionError("the audit ran before --fail-on was validated")

    monkeypatch.setattr(cli, "run_audit", no_audit)
    tex, bib = tmp_path / "p.tex", tmp_path / "p.bib"
    tex.write_text("\\cite{k}", encoding="utf-8")
    bib.write_text("@article{k, title={T}}", encoding="utf-8")
    result = CliRunner().invoke(app, ["audit", str(tex), str(bib), "--fail-on", "halucinated"])
    assert result.exit_code == 2
    assert "--fail-on must be" in result.output


def test_cli_rejects_fail_on_with_no_network(tmp_path):
    # The parse-only report has no verdicts, so --fail-on would always pass.
    tex, bib = tmp_path / "p.tex", tmp_path / "p.bib"
    tex.write_text("\\cite{k}", encoding="utf-8")
    bib.write_text("@article{k, title={T}}", encoding="utf-8")
    result = CliRunner().invoke(
        app, ["audit", str(tex), str(bib), "--no-network", "--fail-on", "hallucinated"]
    )
    assert result.exit_code == 2
    assert "--fail-on cannot be combined with --no-network" in result.output


def test_a_full_length_list_with_a_spelling_variant_omits_nobody():
    # HALLMARK c96d1587797e (VALID): the record writes 'Brandon RichardWebster'. Under
    # --partial-authors error this was reported as an omission and scored as a hallucination.
    cited = ["Brian Hu", "Paul Tunison", "Brandon Richard Webster", "Anthony Hoogs"]
    record = ["Brian Hu", "Paul Tunison", "Brandon RichardWebster", "Anthony Hoogs"]
    assert omitted_authors(cited, record) == []
    assert omitted_authors(cited[:2], record) == ["Brandon RichardWebster", "Anthony Hoogs"]


# ── surname scoring (`author_overlap`) ───────────────────────────────────────


def test_organizational_authors_are_scored_by_their_distinctive_words():
    from reference_audit.matching.names import author_overlap

    # the shared generic noun must not make unrelated bodies match (ai4x2028hallucinated regression)
    assert author_overlap(["AI4X Program Committee"], ["F25 Committee"]) < 0.5
    assert author_overlap(["CMS Collaboration"], ["ATLAS Collaboration"]) < 0.5
    assert author_overlap(["The ATLAS Collaboration"], ["ATLAS Collaboration"]) == 1.0
    # an organization never matches a person, even one whose surname is its distinctive word
    assert author_overlap(["Program Committee"], ["Jane Program"]) == 0.0


def test_a_compound_surname_scores_as_its_part():
    from reference_audit.matching.names import author_overlap

    # schutt2017schnet: the bib keeps both surnames, OpenAlex only the first
    bib = ["Schütt, Kristof", "Sauceda Felix, Huziel Enoc"]
    assert author_overlap(bib, ["Kristof T. Schütt", "Huziel E. Sauceda"]) == 1.0
    # a different surname that merely shares letters still does not match
    assert author_overlap(["Sauceda Felix, Huziel"], ["Maria Felici"]) < 0.8


def test_cli_can_fail_on_unresolved(tmp_path, monkeypatch):
    from reference_audit.models import AuditReport

    report = AuditReport(entries=[], summary={"verdicts": {"unresolved": 1}})
    bib = tmp_path / "refs.bib"
    bib.write_text("@article{k,title={T}}", encoding="utf-8")
    monkeypatch.setattr(cli, "run_audit", lambda *args, **kwargs: report)
    result = CliRunner().invoke(app, ["audit", str(bib), "--fail-on", "unresolved"])
    assert result.exit_code == 1
