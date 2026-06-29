"""FeatureVector: interpretable per-(entry, candidate) signals.

Decomposes `_best_match`'s scalar into named features. M2 wires title/author/year/id_agreement +
the advisory `composite`; the distinct-work detectors (prefix_trap, author_set_jaccard,
pages_conflict) are implemented here too and become load-bearing in M5's SAME-OBJECT rule.
"""

from __future__ import annotations

import re
from typing import Literal

from anyascii import anyascii
from rapidfuzz import fuzz

from reference_audit.models import BibEntry, FeatureVector, Identifiers, SourceRecord
from reference_audit.matching.names import author_overlap, author_set_jaccard, author_subset


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", anyascii(t or "").lower())).strip()


def title_ratio(a: str, b: str) -> float:
    """Length-guarded title similarity.

    `token_set_ratio` returns 100 when the shorter title's tokens are a *subset* of the longer's
    ("Fitness Landscapes" ⊂ "Fitness Landscapes and the Origin of Species"), which would falsely
    match a different, shorter-titled work. So token_set_ratio is only trusted when the two titles
    have comparable token counts; otherwise fall back to order-aware ratios that penalize the
    length gap.
    """
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return 0.0
    base = max(fuzz.ratio(na, nb), fuzz.token_sort_ratio(na, nb))
    ta, tb = len(na.split()), len(nb.split())
    length_ratio = min(ta, tb) / max(ta, tb) if max(ta, tb) else 0.0
    if length_ratio >= 0.7:  # comparable lengths → token_set reordering bonus is safe
        base = max(base, fuzz.token_set_ratio(na, nb))
    return base / 100.0


def _tokens(t: str) -> list[str]:
    return [w for w in _norm_title(t).split() if w]


def title_prefix_trap(a: str, b: str, *, tail_threshold: float) -> bool:
    """High shared leading-token overlap but divergent tails (bagrov vs kravchenko, T2a)."""
    ta, tb = _tokens(a), _tokens(b)
    if len(ta) < 4 or len(tb) < 4:
        return False
    shared_prefix = 0
    for x, y in zip(ta, tb):
        if x == y:
            shared_prefix += 1
        else:
            break
    if shared_prefix < 3:
        return False
    tail_a, tail_b = set(ta[shared_prefix:]), set(tb[shared_prefix:])
    if not tail_a or not tail_b:
        return False
    tail_jaccard = len(tail_a & tail_b) / len(tail_a | tail_b)
    return tail_jaccard < tail_threshold


def year_factor(qy: int | None, iy: int | None) -> float:
    if qy is None or iy is None:
        return 1.0
    delta = abs(qy - iy)
    return 1.0 / (1.0 + (delta / 3.0) ** 2)  # ~1.0 at ±1, never a hard gate (preprint↔published)


def _published_doi(doi: str | None) -> str | None:
    """A DOI that identifies a published artifact (an arXiv DataCite DOI is the preprint id, not a
    competing published DOI, so it must not register as a DOI conflict)."""
    if not doi or doi.startswith("10.48550/arxiv"):
        return None
    return doi


def id_agreement(a: Identifiers, b: Identifiers) -> Literal["match", "conflict", "absent"]:
    """Opaque-token compare across shared id kinds: match wins; conflict if a shared kind disagrees.

    arXiv DataCite DOIs are compared as arXiv ids, not as published DOIs — a published DOI vs an
    arXiv preprint DOI is a version relationship, not a conflict.
    """
    saw_shared = False
    pairs = (
        (_published_doi(a.doi), _published_doi(b.doi)),
        (a.isbn13, b.isbn13),
        (a.arxiv_id, b.arxiv_id),
        (a.pmid, b.pmid),
        (a.openalex, b.openalex),
    )
    for x, y in pairs:
        if x and y:
            saw_shared = True
            if x == y:
                return "match"
    return "conflict" if saw_shared else "absent"


_PAGE_RANGE_RE = re.compile(r"(\d+)\s*[-–—]+\s*(\d+)")


def _page_range(pages: str) -> tuple[int, int] | None:
    m = _PAGE_RANGE_RE.search(pages or "")
    if not m:
        return None
    lo, hi = int(m.group(1)), int(m.group(2))
    return (lo, hi) if lo <= hi else (hi, lo)


def _pages_conflict(pages_a: str, venue_a: str, pages_b: str, venue_b: str) -> bool:
    ra, rb = _page_range(pages_a), _page_range(pages_b)
    if not ra or not rb:
        return False
    same_venue = title_ratio(venue_a, venue_b) > 0.8 if venue_a and venue_b else True
    if not same_venue:
        return False
    return rb[1] < ra[0] or ra[1] < rb[0]  # disjoint


def pages_conflict(entry: BibEntry, record: SourceRecord) -> bool:
    """Disjoint page ranges in the same venue (laughlin consecutive pages, T2c)."""
    return _pages_conflict(entry.pages, entry.venue, record.pages, record.venue)


def pages_conflict_records(a: SourceRecord, b: SourceRecord) -> bool:
    """Record-vs-record page conflict (used by SAME-OBJECT clustering)."""
    return _pages_conflict(a.pages, a.venue, b.pages, b.venue)


def venue_compatible(entry_venue: str, record_venue: str) -> float:
    if not entry_venue or not record_venue:
        return 1.0  # missing venue never penalizes
    return 0.9 + 0.1 * (fuzz.partial_ratio(_norm_title(entry_venue), _norm_title(record_venue)) / 100.0)


def compute_features(entry: BibEntry, record: SourceRecord, *, tail_threshold: float) -> FeatureVector:
    t_ratio = title_ratio(entry.title, record.title)
    a_overlap = author_overlap(entry.authors, record.authors)
    yf = year_factor(entry.year, record.year)
    vc = venue_compatible(entry.venue, record.venue)
    composite = t_ratio * (0.5 + 0.5 * a_overlap) * yf * vc
    return FeatureVector(
        title_ratio=t_ratio,
        title_prefix_trap=title_prefix_trap(entry.title, record.title, tail_threshold=tail_threshold),
        author_overlap=a_overlap,
        author_set_jaccard=author_set_jaccard(entry.authors, record.authors),
        author_subset=author_subset(entry.authors, record.authors),
        year_factor=yf,
        venue_compatible=vc,
        id_agreement=id_agreement(entry.ids, record.ids),
        pages_conflict=pages_conflict(entry, record),
        composite=composite,
    )
