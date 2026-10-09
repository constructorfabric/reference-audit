"""Person-level author matching (`matching/names.py`): the author check and the pooling guard.

Cases are from the HALLMARK dev_public run (pipeline 0.21), where a surname-only fuzzy match made
real authors look fabricated and let a fabricated one pass.
"""

import pytest
from typer.testing import CliRunner

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


def test_a_full_length_list_with_a_spelling_variant_omits_nobody():
    # HALLMARK c96d1587797e (VALID): the record writes 'Brandon RichardWebster'. Under
    # --partial-authors error this was reported as an omission and scored as a hallucination.
    cited = ["Brian Hu", "Paul Tunison", "Brandon Richard Webster", "Anthony Hoogs"]
    record = ["Brian Hu", "Paul Tunison", "Brandon RichardWebster", "Anthony Hoogs"]
    assert omitted_authors(cited, record) == []
    assert omitted_authors(cited[:2], record) == ["Brandon RichardWebster", "Anthony Hoogs"]
