"""Edition-aware book verification, grounded in Open Library editions.

A .bib book entry usually cites a *specific edition* — a (year, publisher) pair, sometimes an ISBN.
The generic step-1 matcher pools whatever records the sources return, and for books that often
includes a *newer* edition pulled from Crossref (e.g. a 2018 Routledge reprint of a 1976 book). The
generic field check then compares the cited original edition's year/publisher against that newer
record and wrongly flags them as mistakes.

This module fixes that by working off the actual Open Library editions of the book:

  1. `match_cited_edition` finds the edition the .bib is citing (by ISBN, else year+publisher, else
     year). When found, the cited edition "checks out" and its own year/publisher are the canonical
     values to verify against — so the original edition is no longer flagged against a reprint. When
     *not* found (Open Library doesn't have the book, or not that edition), the caller reports the
     gap to the user rather than silently passing — see the reliability contract.
  2. `latest_edition` / `better_edition_note` then point at the most recent edition as a possible
     better version to cite (step 2).

Pure and network-free: the editions are fetched by the Open Library adapter and passed in.
"""

from __future__ import annotations

import re

from reference_audit.models import BibEntry, SourceRecord


def _fold(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _publisher_compatible(bib_publisher: str, edition_publisher: str) -> bool:
    """The cited publisher matches the edition's when one folded name contains the other
    ('W. A. Benjamin' ⊆ 'W. A. Benjamin, Advanced Book Program')."""
    a, b = _fold(bib_publisher), _fold(edition_publisher)
    return bool(a and b and (a == b or a in b or b in a))


def match_cited_edition(
    entry: BibEntry, editions: list[SourceRecord]
) -> SourceRecord | None:
    """The Open Library edition the .bib cites, or None if no edition corresponds.

    Strongest signal first: the cited ISBN, then year+publisher agreement, then year alone.
    """
    if not editions:
        return None
    if entry.ids.isbn13:
        for e in editions:
            if e.ids.isbn13 and e.ids.isbn13 == entry.ids.isbn13:
                return e
    if entry.year is not None:
        if entry.publisher:
            for e in editions:
                if (
                    e.year == entry.year
                    and e.publisher
                    and _publisher_compatible(entry.publisher, e.publisher)
                ):
                    return e
        for e in editions:
            if e.year == entry.year:
                return e
    return None


def latest_edition(editions: list[SourceRecord]) -> SourceRecord | None:
    """The most recently published edition among those carrying a year."""
    dated = [e for e in editions if e.year is not None]
    return max(dated, key=lambda e: e.year) if dated else None


def better_edition_note(
    matched: SourceRecord | None, latest: SourceRecord | None
) -> str | None:
    """A step-2 upgrade notice when a later edition than the cited one exists; else None."""
    if matched is None or latest is None or matched.year is None or latest.year is None:
        return None
    if latest.year <= matched.year:
        return None
    where = ", ".join(p for p in (str(latest.year), latest.publisher) if p)
    isbn = f" (isbn {latest.ids.isbn13})" if latest.ids.isbn13 else ""
    return (
        f"citing the {matched.year} edition; a newer edition is available: {where}{isbn}"
        " — verify whether you should cite the latest edition"
    )


def describe_cited_edition(entry: BibEntry) -> str:
    """Human description of the edition the .bib cites, for the could-not-verify report."""
    bits = [str(entry.year)] if entry.year else []
    if entry.publisher:
        bits.append(entry.publisher)
    if entry.ids.isbn13:
        bits.append(f"isbn {entry.ids.isbn13}")
    return ", ".join(bits) or "this edition"
