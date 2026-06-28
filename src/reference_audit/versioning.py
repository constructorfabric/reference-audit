"""Step 2: better-version detection.

For each exactly-matched artifact, report whether the .bib entry is citing a
suboptimal version:
  - Paper citing an arXiv preprint when a real published DOI is known.
  - Book with no edition specifier (or a low edition number) when multiple
    editions are known to exist in OpenLibrary.
"""

from __future__ import annotations

import re

from reference_audit.models import BibEntry, EntryType, MatchedArtifact

_PREPRINT_DOI_PREFIX = "10.48550/arxiv"

_ORDINALS: dict[str, int] = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}

_PAPER_TYPES = {EntryType.ARTICLE, EntryType.INPROCEEDINGS, EntryType.MISC}
_BOOK_TYPES = {EntryType.BOOK, EntryType.INCOLLECTION}


def _is_preprint_doi(doi: str) -> bool:
    return doi.lower().startswith(_PREPRINT_DOI_PREFIX)


def _entry_cites_preprint(entry: BibEntry) -> bool:
    """True if the entry's primary identifier is a preprint (arXiv) reference."""
    if entry.ids.doi:
        return _is_preprint_doi(entry.ids.doi)
    return bool(entry.ids.arxiv_id)


def _find_published_doi(artifact: MatchedArtifact) -> str | None:
    """Return the first real published DOI found across all artifact versions."""
    for record in artifact.versions:
        doi = record.ids.doi
        if doi and not record.is_preprint and not _is_preprint_doi(doi):
            return doi
    return None


def _parse_edition_num(s: str) -> int | None:
    """Parse '2', '2nd', 'Second edition', etc. to an int. Returns None if unparseable."""
    s = s.strip()
    m = re.match(r"^(\d+)", s)
    if m:
        return int(m.group(1))
    first_word = s.lower().split()[0] if s else ""
    return _ORDINALS.get(first_word)


def better_version_notes(entry: BibEntry, artifact: MatchedArtifact) -> list[str]:
    """Return upgrade notices for this entry (empty list means best version is already cited).

    Called after step-1 verdict is exactly_one; the artifact is confirmed to
    correspond to the entry so all records in artifact.versions are relevant.
    """
    notes: list[str] = []

    if entry.entry_type in _PAPER_TYPES and _entry_cites_preprint(entry):
        pub_doi = _find_published_doi(artifact)
        if pub_doi:
            notes.append(f"citing preprint; published version available: doi:{pub_doi}")

    if entry.entry_type in _BOOK_TYPES:
        best = artifact.best_record
        # best.edition from OpenLibrary = edition_count (total editions known).
        # If it is > 1 the work has multiple editions; verify the cited one is current.
        if best is not None and best.edition is not None and best.edition > 1:
            edition_str = entry.raw_fields.get("edition", "")
            if not edition_str:
                notes.append(
                    f"{best.edition} editions known in OpenLibrary; "
                    "add edition= field and verify you cite the latest"
                )
            else:
                cited = _parse_edition_num(edition_str)
                if cited is not None and best.edition > cited:
                    notes.append(
                        f"citing edition {cited}; {best.edition} editions known in OpenLibrary"
                        " — a later edition may be available"
                    )

    return notes
