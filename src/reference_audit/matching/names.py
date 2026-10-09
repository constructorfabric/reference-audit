"""Author-name matching (variant- and transliteration-aware).

Distilled from `sciwrite-lint/api.py:_name_variants/_author_overlap`. Names may be "Last, First",
"First Last", initials, or transliterated (anyascii) — we compare on normalized last names with a
fuzzy fallback, so "Vanchurin, Vitaly" ≈ "Vitaly Vanchurin" and "Müller" ≈ "Mueller".
"""

from __future__ import annotations

import re

from anyascii import anyascii
from rapidfuzz import fuzz

from reference_audit.parsing.entities import decode_html_entities


def _norm(text: str) -> str:
    # Decode HTML references first: "d&apos;Amore" must compare as "d'Amore", not "dapos amore".
    text = anyascii(decode_html_entities(text or ""))
    return re.sub(r"[^a-z\s,.-]", "", text.lower()).strip()


# The BibTeX `and others` convention (and a written-out "et al.") is a *truncation marker*, not a
# real author. Left in, the phantom surname "others" both drags `author_overlap` down and breaks the
# subset check (an intentionally abbreviated list is no longer ⊆ the full author list), which then
# trips the distinct-author-set veto and forces needless adjudication. Drop it everywhere.
_ETAL_MARKERS = {"others", "et al", "and others"}


def _is_etal(name: str) -> bool:
    return _norm(name).replace(".", "").strip() in _ETAL_MARKERS


def _named(authors: list[str]) -> list[str]:
    """Author list with any et-al./`others` truncation marker removed."""
    return [a for a in authors if not _is_etal(a)]


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
    return {ln for a in _named(authors) if (ln := last_name(a))}


def author_overlap(query_authors: list[str], item_authors: list[str]) -> float:
    """Average best per-query-author surname match in [0, 1]."""
    query_authors, item_authors = _named(query_authors), _named(item_authors)
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


# ── person-level matching (the author check) ─────────────────────────────────
#
# The surname-only fuzzy match above suits scoring, where an average over the list absorbs one bad
# name. The author check names individual people, so it compares whole names, order-free: databases
# disagree on name order ('Li Tian' / 'Tian Li', OpenAlex's 'Moriano Pablo') and on how much of a
# compound surname they keep ('Lierni Sestorain' / 'Sestorain Saralegui, Lierni', 'Carlos Riquelme
# Ruiz' / 'Carlos Riquelme'). A surname-only fuzzy match also lets a fabricated author through on a
# near-namesake ('Carreira' ≈ 'Barreira' at 0.875).

_NAME_SPLIT_RE = re.compile(r"[\s,.\-‐]+")
_UMLAUT_RE = re.compile(r"([aou])e")


def _name_tokens(name: str, *, join_hyphens: bool = False) -> tuple[list[str], list[str]]:
    """(full tokens, initials) of a name, lower-cased and transliterated; order is not kept.

    A hyphen splits by default ('Gontijo-Lopes' / 'Gontijo Lopes'); `join_hyphens` closes it instead,
    for given names a source writes without one ('Guo-Sen' / 'Guosen').
    """
    text = _norm(name)
    if join_hyphens:
        text = re.sub(r"(?<=[a-z])[-‐](?=[a-z])", "", text)
    tokens = [t for t in _NAME_SPLIT_RE.split(text) if t]
    return [t for t in tokens if len(t) > 1], [t for t in tokens if len(t) == 1]


def _token_match(a: str, b: str) -> bool:
    """Same name token, allowing umlaut transliteration ('mueller' / 'muller') and a one-letter
    slip in a long token, but not a near-namesake ('carreira' / 'barreira')."""
    if a == b or _UMLAUT_RE.sub(r"\1", a) == _UMLAUT_RE.sub(r"\1", b):
        return True
    return min(len(a), len(b)) >= 5 and fuzz.ratio(a, b) >= 90


def same_person(a: str, b: str) -> bool:
    """Whether two author strings can name the same person.

    Every full token of the shorter name must appear in the other (in any order); the longer name may
    carry extra tokens (a second surname, a middle name). Each initial of the shorter name must start
    some token of the other ('D.P. Woodruff' / 'David P. Woodruff', but not 'J. Smith' / 'Adam Smith').
    """
    return any(
        _tokens_agree(_name_tokens(a, join_hyphens=j), _name_tokens(b, join_hyphens=j))
        for j in (False, True)
    )


def _tokens_agree(a: tuple[list[str], list[str]], b: tuple[list[str], list[str]]) -> bool:
    if not (a[0] or a[1]) or not (b[0] or b[1]):
        return False
    # An initials-only name ('A. B.') sorts first and agrees when its initials start the other's.
    (short_full, short_init), (long_full, long_init) = sorted((a, b), key=lambda t: len(t[0]))
    long_tokens = long_full + long_init
    for tok in short_full:
        if not any(_token_match(tok, other) for other in long_full):
            # a given name may be an initial on the other side ('David' / 'D.')
            if not (tok[0] in long_init and len(short_full) > 1):
                return False
    return all(any(t.startswith(i) for t in long_tokens) for i in short_init)


def check_cited_authors(
    bib_authors: list[str], record_author_lists: list[list[str]]
) -> tuple[list[str], list[str]]:
    """(missing, unchecked): cited authors who appear in none of the matched work's records, and
    cited authors no record reaches far enough to check.

    Every source record of the work is consulted, so one source's defect (S2's 'Wenhan Wang' for
    Wenhan Yang, OpenAlex's surname-less 'Ed H.') does not make a real author look fabricated.

    A record may be truncated (OpenAlex stops at 100 authors). When every record is shorter than the
    cited list and the longest one is, in order, its leading part, the cited authors past its end are
    `unchecked`: the record may have been cut there. When the record and the cited list disagree, the
    list was not truncated but replaced, and every cited author is checked.
    """
    bib = _named(bib_authors)
    lists = [named for lst in record_author_lists if (named := _named(lst))]
    if not bib or not lists:
        return [], []
    pool = [name for lst in lists for name in lst]
    missing = [i for i, a in enumerate(bib) if not any(same_person(a, other) for other in pool)]
    longest = max(lists, key=len)
    k = len(longest)
    if missing and k < len(bib) and all(same_person(x, y) for x, y in zip(longest, bib)):
        return [bib[i] for i in missing if i < k], [bib[i] for i in missing if i >= k]
    return [bib[i] for i in missing], []


def authors_missing(bib_authors: list[str], record_author_lists: list[list[str]]) -> list[str]:
    """Cited authors who appear in none of the matched work's records (see `check_cited_authors`)."""
    return check_cited_authors(bib_authors, record_author_lists)[0]


def omitted_authors(bib_authors: list[str], record_authors: list[str]) -> list[str]:
    """Authors of the record that the cited list leaves out, when it does not say it is shortened.

    An `and others` / `et al.` in the cited list is an explicit truncation, so nothing is omitted.
    """
    if any(_is_etal(a) for a in bib_authors):
        return []
    bib = _named(bib_authors)
    if not bib:
        return []
    return [r for r in _named(record_authors) if not any(same_person(r, b) for b in bib)]


def authors_compatible(a: list[str], b: list[str], threshold: float = 0.8) -> bool:
    """Whether two records' author lists can belong to one work: at least `threshold` of the
    shorter list are people on the longer one. Unknown (an empty list) counts as compatible."""
    a, b = _named(a), _named(b)
    if not a or not b:
        return True
    short, long_ = sorted((a, b), key=len)
    found = sum(1 for x in short if any(same_person(x, y) for y in long_))
    return found / len(short) >= threshold
