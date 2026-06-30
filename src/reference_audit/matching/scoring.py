"""Bucketing: auto_accept / auto_reject / adjudicate — the funnel before any LLM call.

The 100%-match short-circuit (README "unless a returned record is a 100% match, use an LLM")
reads the feature VECTOR, not the scalar: an id match alone is not enough (a hallucinated DOI that
resolves must still pass title+author), and a prefix-trap/pages-conflict forces adjudication even
at high composite (stops T2 from auto-accepting).
"""

from __future__ import annotations

from typing import Literal

from reference_audit.config import AuditConfig
from reference_audit.models import FeatureVector

Bucket = Literal["auto_accept", "auto_reject", "adjudicate"]


def bucket(features: FeatureVector, config: AuditConfig, *, entry_has_id: bool = True) -> Bucket:
    """Bucket one (entry, candidate) by its feature vector.

    `entry_has_id` enables the strict backfill path: when the bib entry carries no *strong* anchor
    (DOI / ISBN / arXiv / OpenAlex / Google Books — a bare URL does not count, see
    `Identifiers.has_strong_id`), a near-exact title + strong authors (and no distinct-work veto) is
    accepted so the matched DOI can be backfilled. The threshold is stricter than the id-based path
    because there is no identifier to anchor the match.
    """
    # A low author-set overlap forces adjudication ONLY when neither set is a subset of the other:
    # a subset is consistent with an abbreviated "et al." list (same work), not a distinct work.
    author_set_distinct = (
        features.author_set_jaccard < config.author_set_distinct_jaccard
        and not features.author_subset
    )
    authors_ok = features.author_overlap >= config.author_accept or features.author_subset

    # A disjoint page range in the same venue normally signals a DISTINCT work (laughlin T2c: two
    # different-titled papers on consecutive pages). But when the title is a near-exact match AND the
    # authors agree, two different works is not a credible explanation — the likely one is a wrong
    # `pages` field in the citation (soros2014: cited 306--313 vs the identical paper's canonical
    # 793--800). Treat that as a field error to be REPORTED by the field check downstream, not as
    # grounds to call a real, title-and-author-identical paper a possible hallucination. (prefix_trap
    # stays load-bearing: it fires on divergent title TAILS, i.e. when the titles are NOT near-exact.)
    title_authors_lock = features.title_ratio >= config.title_backfill and authors_ok
    pages_conflict_distinct = features.pages_conflict and not title_authors_lock
    forced_adjudicate = (
        features.title_prefix_trap or pages_conflict_distinct or author_set_distinct
    )

    # Path A — entry has an identifier that the candidate matches (authoritative; README: IDs
    # uniquely identify). Accept even if the DB holds a subset of authors (wilson1974/Kogut); the
    # title gate still rejects a hallucinated DOI resolving to a different paper (T4-by-bad-id).
    if (
        features.id_agreement == "match"
        and features.title_ratio >= config.title_accept
        and authors_ok
        and not forced_adjudicate
    ):
        return "auto_accept"

    # Path B — entry has NO identifier; accept a near-exact title + strong authors to backfill the
    # candidate's DOI/ISBN. Stricter title floor; all distinct-work vetoes still apply (keeps T2a's
    # bagrov/kravchenko prefix-trap from auto-accepting).
    if (
        not entry_has_id
        and features.id_agreement != "conflict"
        and features.title_ratio >= config.title_backfill
        and features.author_overlap >= config.author_accept
        and not forced_adjudicate
    ):
        return "auto_accept"

    if features.id_agreement != "match" and features.composite < config.composite_reject:
        return "auto_reject"

    return "adjudicate"
