"""Step 2: better-version detection — unit tests.

All tests use mocked MatchedArtifact objects; no network calls.
"""

from __future__ import annotations

import pytest

from reference_audit.models import (
    BibEntry,
    EntryType,
    Identifiers,
    MatchedArtifact,
    SourceRecord,
)
from reference_audit.versioning import (
    _entry_cites_preprint,
    _find_published_doi,
    _parse_edition_num,
    better_version_notes,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _entry(
    entry_type: EntryType = EntryType.ARTICLE,
    doi: str | None = None,
    arxiv_id: str | None = None,
    raw_fields: dict | None = None,
    year: int | None = None,
) -> BibEntry:
    return BibEntry(
        key="k",
        entry_type=entry_type,
        title="T",
        year=year,
        ids=Identifiers(doi=doi, arxiv_id=arxiv_id),
        raw_fields=raw_fields or {},
    )


def _record(
    *,
    doi: str | None = None,
    arxiv_id: str | None = None,
    is_preprint: bool = False,
    edition: int | None = None,
    year: int | None = None,
) -> SourceRecord:
    return SourceRecord(
        source="test",
        title="T",
        year=year,
        ids=Identifiers(doi=doi, arxiv_id=arxiv_id),
        is_preprint=is_preprint,
        edition=edition,
    )


def _artifact(*records: SourceRecord) -> MatchedArtifact:
    best = max(
        records,
        key=lambda r: (0 if r.is_preprint else 1, 1 if r.ids.doi else 0, r.citation_count),
    )
    return MatchedArtifact(
        records=list(records),
        versions=list(records),
        best_record=best,
        merged_ids=Identifiers(),
    )


# ── _entry_cites_preprint ────────────────────────────────────────────────────

def test_arxiv_only_is_preprint():
    assert _entry_cites_preprint(_entry(arxiv_id="2401.12345")) is True


def test_published_doi_not_preprint():
    assert _entry_cites_preprint(_entry(doi="10.1234/pub")) is False


def test_arxiv_doi_is_preprint():
    assert _entry_cites_preprint(_entry(doi="10.48550/arxiv.2401.12345")) is True


def test_no_ids_not_preprint():
    assert _entry_cites_preprint(_entry()) is False


# ── _find_published_doi ──────────────────────────────────────────────────────

def test_find_published_doi_from_versions():
    art = _artifact(
        _record(arxiv_id="2401.00001", is_preprint=True),
        _record(doi="10.1234/pub"),
    )
    assert _find_published_doi(art) == "10.1234/pub"


def test_arxiv_doi_not_returned_as_published():
    art = _artifact(_record(doi="10.48550/arxiv.2401.00001", is_preprint=True))
    assert _find_published_doi(art) is None


def test_no_published_record_returns_none():
    art = _artifact(_record(arxiv_id="2401.00001", is_preprint=True))
    assert _find_published_doi(art) is None


# ── _parse_edition_num ───────────────────────────────────────────────────────

@pytest.mark.parametrize("s,expected", [
    ("2", 2),
    ("2nd", 2),
    ("2nd edition", 2),
    ("Second", 2),
    ("second edition", 2),
    ("Third", 3),
    ("10th", 10),
    ("", None),
    ("revised", None),
])
def test_parse_edition_num(s, expected):
    assert _parse_edition_num(s) == expected


# ── better_version_notes: paper preprint→published ───────────────────────────

def test_preprint_entry_with_published_version_flagged():
    e = _entry(arxiv_id="2401.12345")
    art = _artifact(
        _record(arxiv_id="2401.12345", is_preprint=True),
        _record(doi="10.1234/pub"),
    )
    notes = better_version_notes(e, art)
    assert len(notes) == 1
    assert "published version available" in notes[0]
    assert "10.1234/pub" in notes[0]


def test_arxiv_doi_entry_with_published_version_flagged():
    e = _entry(doi="10.48550/arxiv.2401.12345")
    art = _artifact(_record(doi="10.9999/real"))
    notes = better_version_notes(e, art)
    assert any("published version available" in n for n in notes)


def test_published_doi_entry_no_note():
    e = _entry(doi="10.1234/pub")
    art = _artifact(
        _record(arxiv_id="2401.12345", is_preprint=True),
        _record(doi="10.1234/pub"),
    )
    assert better_version_notes(e, art) == []


def test_preprint_only_artifact_no_note():
    e = _entry(arxiv_id="2401.12345")
    art = _artifact(_record(arxiv_id="2401.12345", is_preprint=True))
    assert better_version_notes(e, art) == []


# ── better_version_notes: newer arXiv version (no published DOI) ───────────────

def test_newer_arxiv_version_flagged():
    # kumar2024automating: cites v1 (2024 = id-encoded submission year); canonical/latest is 2025.
    e = _entry(entry_type=EntryType.MISC, arxiv_id="2412.17799", year=2024)
    art = _artifact(_record(arxiv_id="2412.17799", is_preprint=True, year=2025))
    notes = better_version_notes(e, art)
    assert len(notes) == 1
    assert "newer version" in notes[0]
    assert "2025" in notes[0]


def test_newer_arxiv_version_via_arxiv_doi():
    e = _entry(entry_type=EntryType.MISC, doi="10.48550/arxiv.2412.17799", year=2024)
    art = _artifact(_record(doi="10.48550/arxiv.2412.17799", is_preprint=True, year=2025))
    assert any("newer version" in n for n in better_version_notes(e, art))


def test_preprint_same_year_no_newer_version_note():
    # Canonical year equals the cited (original) version year → nothing newer to upgrade to.
    e = _entry(entry_type=EntryType.MISC, arxiv_id="2412.17799", year=2024)
    art = _artifact(_record(arxiv_id="2412.17799", is_preprint=True, year=2024))
    assert better_version_notes(e, art) == []


def test_preprint_year_not_original_no_version_note():
    # Cited year (2020) is not the id-encoded submission year (2024): a possible bib error, left to
    # the field check — not mislabeled here as "newer version available".
    e = _entry(entry_type=EntryType.MISC, arxiv_id="2412.17799", year=2020)
    art = _artifact(_record(arxiv_id="2412.17799", is_preprint=True, year=2025))
    assert better_version_notes(e, art) == []


def test_misc_preprint_flagged():
    e = _entry(entry_type=EntryType.MISC, arxiv_id="2401.00001")
    art = _artifact(
        _record(arxiv_id="2401.00001", is_preprint=True),
        _record(doi="10.5555/conf.42"),
    )
    notes = better_version_notes(e, art)
    assert any("published version available" in n for n in notes)


def test_inproceedings_preprint_flagged():
    e = _entry(entry_type=EntryType.INPROCEEDINGS, arxiv_id="2401.00001")
    art = _artifact(
        _record(arxiv_id="2401.00001", is_preprint=True),
        _record(doi="10.5555/conf.42"),
    )
    notes = better_version_notes(e, art)
    assert any("published version available" in n for n in notes)


# ── better_version_notes: book edition ──────────────────────────────────────

def test_book_no_edition_field_multiple_editions_flagged():
    e = _entry(entry_type=EntryType.BOOK)
    art = _artifact(_record(edition=4))
    notes = better_version_notes(e, art)
    assert len(notes) == 1
    assert "4 editions" in notes[0]
    assert "edition= field" in notes[0]


def test_book_single_edition_no_note():
    e = _entry(entry_type=EntryType.BOOK)
    art = _artifact(_record(edition=1))
    assert better_version_notes(e, art) == []


def test_book_no_edition_data_no_note():
    e = _entry(entry_type=EntryType.BOOK)
    art = _artifact(_record())  # edition=None
    assert better_version_notes(e, art) == []


def test_book_old_edition_with_newer_known():
    e = _entry(entry_type=EntryType.BOOK, raw_fields={"edition": "1"})
    art = _artifact(_record(edition=4))
    notes = better_version_notes(e, art)
    assert any("later edition may be available" in n for n in notes)


def test_book_edition_matches_count_no_note():
    # Citing edition 3, edition_count=3 → no newer edition possible.
    e = _entry(entry_type=EntryType.BOOK, raw_fields={"edition": "3"})
    art = _artifact(_record(edition=3))
    assert better_version_notes(e, art) == []


def test_book_ordinal_edition_parsed():
    e = _entry(entry_type=EntryType.BOOK, raw_fields={"edition": "Second"})
    art = _artifact(_record(edition=4))
    notes = better_version_notes(e, art)
    assert any("later edition" in n for n in notes)


def test_incollection_edition_flagged():
    e = _entry(entry_type=EntryType.INCOLLECTION, raw_fields={"edition": "1st"})
    art = _artifact(_record(edition=3))
    notes = better_version_notes(e, art)
    assert any("later edition" in n for n in notes)


def test_article_ignores_book_edition_logic():
    # Articles shouldn't trigger edition warnings even if the record has edition data.
    e = _entry(entry_type=EntryType.ARTICLE, doi="10.1/x")
    art = _artifact(_record(doi="10.1/x", edition=5))
    assert better_version_notes(e, art) == []
