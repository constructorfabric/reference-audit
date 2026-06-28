"""Edition-aware book check — unit tests for the pure selection + better-version logic.

No network: editions are passed in as SourceRecords (as the Open Library adapter would return).
"""

from __future__ import annotations

from reference_audit.bookcheck import (
    better_edition_note,
    describe_cited_edition,
    latest_edition,
    match_cited_edition,
)
from reference_audit.models import BibEntry, EntryType, Identifiers, SourceRecord


def _entry(
    *,
    year: int | None = None,
    publisher: str = "",
    isbn13: str | None = None,
    entry_type: EntryType = EntryType.BOOK,
) -> BibEntry:
    return BibEntry(
        key="k",
        entry_type=entry_type,
        title="Modern Theory of Critical Phenomena",
        authors=["Ma, Shang-Keng"],
        year=year,
        publisher=publisher,
        ids=Identifiers(isbn13=isbn13),
    )


def _edition(*, year: int | None, publisher: str = "", isbn13: str | None = None) -> SourceRecord:
    return SourceRecord(
        source="openlibrary",
        title="Modern Theory of Critical Phenomena",
        year=year,
        publisher=publisher,
        ids=Identifiers(isbn13=isbn13),
    )


# the real Open Library editions of the pilot book, across its split work records
_MA_EDITIONS = [
    _edition(year=1976, publisher="W. A. Benjamin, Advanced Book Program", isbn13="9780805366709"),
    _edition(year=2000, publisher="Perseus Pub."),
    _edition(year=2018, publisher="Taylor & Francis Group", isbn13="9780429498886"),
]


# ── match_cited_edition ──────────────────────────────────────────────────────


def test_match_by_isbn():
    e = _entry(year=1976, publisher="W. A. Benjamin, Advanced Book Program", isbn13="9780805366709")
    assert match_cited_edition(e, _MA_EDITIONS).year == 1976


def test_match_by_isbn_wins_over_year_only():
    # ISBN points at the 1976 edition even though another edition could match on year alone.
    e = _entry(year=2018, isbn13="9780805366709")  # mismatched year, but ISBN is decisive
    assert match_cited_edition(e, _MA_EDITIONS).ids.isbn13 == "9780805366709"


def test_match_by_year_and_publisher_substring():
    # mabook: no ISBN, publisher 'W. A. Benjamin' ⊆ the edition's fuller name.
    e = _entry(year=1976, publisher="W. A. Benjamin")
    m = match_cited_edition(e, _MA_EDITIONS)
    assert m.year == 1976 and "Benjamin" in m.publisher


def test_match_by_year_only_when_publisher_differs():
    # gavrilets-like: a publisher typo means no publisher match, but the year still pins the edition.
    e = _entry(year=1976, publisher="W. A. Benjam in")  # typo
    assert match_cited_edition(e, _MA_EDITIONS).year == 1976


def test_no_match_when_year_absent_and_no_isbn():
    assert match_cited_edition(_entry(), _MA_EDITIONS) is None


def test_no_match_when_year_not_among_editions():
    assert match_cited_edition(_entry(year=1999), _MA_EDITIONS) is None


def test_no_match_empty_editions():
    assert match_cited_edition(_entry(year=1976), []) is None


# ── latest_edition ───────────────────────────────────────────────────────────


def test_latest_edition_picks_max_year():
    assert latest_edition(_MA_EDITIONS).year == 2018


def test_latest_edition_ignores_undated():
    eds = [_edition(year=None), _edition(year=1976)]
    assert latest_edition(eds).year == 1976


def test_latest_edition_none_when_all_undated():
    assert latest_edition([_edition(year=None)]) is None


# ── better_edition_note ──────────────────────────────────────────────────────


def test_better_edition_note_when_newer_exists():
    matched = _edition(year=1976, publisher="W. A. Benjamin, Advanced Book Program")
    latest = _edition(year=2018, publisher="Taylor & Francis Group", isbn13="9780429498886")
    note = better_edition_note(matched, latest)
    assert note is not None
    assert "1976 edition" in note
    assert "2018" in note and "Taylor & Francis Group" in note
    assert "9780429498886" in note


def test_no_note_when_cited_is_latest():
    matched = _edition(year=2018, publisher="Taylor & Francis Group")
    assert better_edition_note(matched, matched) is None


def test_no_note_when_latest_older_than_cited():
    matched = _edition(year=2018)
    assert better_edition_note(matched, _edition(year=1976)) is None


def test_no_note_when_missing_data():
    assert better_edition_note(None, _edition(year=2018)) is None
    assert better_edition_note(_edition(year=1976), None) is None
    assert better_edition_note(_edition(year=None), _edition(year=2018)) is None


# ── describe_cited_edition ───────────────────────────────────────────────────


def test_describe_cited_edition():
    e = _entry(year=1976, publisher="W. A. Benjamin", isbn13="9780805366709")
    desc = describe_cited_edition(e)
    assert "1976" in desc and "W. A. Benjamin" in desc and "9780805366709" in desc


def test_describe_cited_edition_fallback():
    assert describe_cited_edition(_entry()) == "this edition"
