"""BibTeX parsing on the real pilot — entry count, the commented twin, and T3 quirks."""

from reference_audit.models import EntryType
from reference_audit.parsing.bib import parse_bib


def _by_key(entries):
    return {e.key: e for e in entries}


def test_pilot_entry_count_and_twin(pilot_bib):
    entries, twins = parse_bib(pilot_bib)
    keys = {e.key for e in entries}
    # 28 real entries; the commented %@misc{bagrov2024visual} is a twin, not audited
    assert len(entries) == 28
    assert "bagrov2024visual" not in keys
    assert [t.key for t in twins] == ["bagrov2024visual"]


def test_commented_twin_is_kravchenko_preprint(pilot_bib):
    _, twins = parse_bib(pilot_bib)
    twin = twins[0]
    assert twin.is_commented is True
    assert twin.ids.arxiv_id == "2408.04076"
    # the malformed "and and" must not leave an empty author
    assert "" not in twin.authors
    assert len(twin.authors) == 4
    assert any("Kravchenko" in a for a in twin.authors)


def test_wolpert_doi_normalized(pilot_bib):
    entries, _ = parse_bib(pilot_bib)
    wolpert = _by_key(entries)["wolpert2007"]
    assert wolpert.ids.doi == "10.1002/cplx.20165"  # https:// prefix stripped


def test_kravchenko_published(pilot_bib):
    entries, _ = parse_bib(pilot_bib)
    k = _by_key(entries)["kravchenko2026"]
    assert k.ids.doi == "10.1177/03010066251384492"
    assert k.year == 2026
    assert k.venue == "Perception"


def test_pugh_has_no_spurious_arxiv(pilot_bib):
    entries, _ = parse_bib(pilot_bib)
    pugh = _by_key(entries)["pugh2016quality"]
    assert pugh.ids.arxiv_id is None
    assert pugh.ids.doi == "10.3389/frobt.2016.00040"


def test_kumar_arxiv_misc(pilot_bib):
    entries, _ = parse_bib(pilot_bib)
    kumar = _by_key(entries)["kumar2024automating"]
    assert kumar.entry_type == EntryType.MISC
    assert kumar.ids.arxiv_id == "2412.17799"


def test_books_without_isbn(pilot_bib):
    entries, _ = parse_bib(pilot_bib)
    by = _by_key(entries)
    for key in ("gavrilets2004", "mabook"):
        assert by[key].entry_type == EntryType.BOOK
        assert by[key].ids.isbn13 is None


def test_latex_accent_decoded(pilot_bib):
    entries, _ = parse_bib(pilot_bib)
    plantec = _by_key(entries)["plantec2023flow"]
    # Cl{\'e}ment -> Clément
    assert any("Clément" in a for a in plantec.authors)
