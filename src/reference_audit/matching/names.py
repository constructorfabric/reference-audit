"""Author-name matching (variant- and transliteration-aware).

Distilled from `sciwrite-lint/api.py:_name_variants/_author_overlap`. Names may be "Last, First",
"First Last", initials, or transliterated (anyascii) — we compare on normalized last names with a
fuzzy fallback, so "Vanchurin, Vitaly" ≈ "Vitaly Vanchurin" and "Müller" ≈ "Mueller".
"""

from __future__ import annotations

import re

from anyascii import anyascii
from rapidfuzz import fuzz


def _norm(text: str) -> str:
    return re.sub(r"[^a-z\s,.-]", "", anyascii(text or "").lower()).strip()


def last_name(name: str) -> str:
    """Best-effort surname extraction handling 'Last, First' and 'First Last'."""
    n = _norm(name)
    if not n:
        return ""
    if "," in n:
        return n.split(",", 1)[0].strip()
    tokens = [t for t in n.replace(".", " ").split() if t]
    return tokens[-1] if tokens else ""


def author_set(authors: list[str]) -> set[str]:
    """Set of normalized surnames (for set-level Jaccard / subset checks)."""
    return {ln for a in authors if (ln := last_name(a))}


def author_overlap(query_authors: list[str], item_authors: list[str]) -> float:
    """Average best per-query-author surname match in [0, 1]."""
    if not query_authors or not item_authors:
        return 0.0
    item_last = [last_name(a) for a in item_authors]
    item_last = [ln for ln in item_last if ln]
    if not item_last:
        return 0.0
    scores: list[float] = []
    for qa in query_authors:
        ql = last_name(qa)
        if not ql:
            continue
        best = max((fuzz.ratio(ql, il) / 100.0 for il in item_last), default=0.0)
        scores.append(best)
    return sum(scores) / len(scores) if scores else 0.0


def author_set_jaccard(query_authors: list[str], item_authors: list[str]) -> float:
    a, b = author_set(query_authors), author_set(item_authors)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def author_subset(query_authors: list[str], item_authors: list[str]) -> bool:
    """True if one author set is contained in the other (beyond spelling) — laughlin asymmetry."""
    a, b = author_set(query_authors), author_set(item_authors)
    if not a or not b:
        return False
    return a <= b or b <= a
