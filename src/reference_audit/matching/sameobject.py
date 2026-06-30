"""SAME-OBJECT clustering over an entry's ACCEPTED candidates → matched artifacts.

`pool.py` already merged the identifier-grounded same-work cases (shared id, version edge, preprint
pair, same edition). What remains for an entry with ≥2 accepted candidates is the genuinely
ambiguous zone: two records with near-identical title+authors but two DISTINCT published DOIs (the
design's V1 veto holds them apart formally — e.g. one paper registered under two DOIs). Those, and
only those, escalate to the `SAME_WORK` LLM tie-break. The distinct-work vetoes V2 (disjoint pages),
V3 (title-prefix trap) and V4 (author-set distinct) are decisive and never reach the LLM.
"""

from __future__ import annotations

from typing import Literal

from reference_audit.cache.store import AuditCache, prompt_hash
from reference_audit.config import AuditConfig
from reference_audit.llm.client import LLMClient, LLMError
from reference_audit.llm.prompts import SAME_WORK_SYSTEM, same_work_user
from reference_audit.matching.features import (
    author_set_jaccard,
    id_agreement,
    pages_conflict_records,
    title_prefix_trap,
    title_ratio,
)
from reference_audit.matching.names import author_overlap, author_subset
from reference_audit.models import (
    CandidateAssessment,
    Identifiers,
    MatchedArtifact,
    SameWorkResult,
    SourceRecord,
)

Relation = Literal["same", "distinct", "ambiguous"]

_SAME_TITLE = 0.90
_SAME_AUTHOR = 0.7


def formal_relation(a: SourceRecord, b: SourceRecord, config: AuditConfig) -> Relation:
    """Formal same-object relation between two accepted records (no LLM)."""
    if id_agreement(a.ids, b.ids) == "match":
        return "same"  # shared exact identifier (M1/M3)
    # Distinct-work vetoes (decisive).
    if pages_conflict_records(a, b):
        return "distinct"  # V2
    if title_prefix_trap(a.title, b.title, tail_threshold=config.prefix_trap_tail_jaccard):
        return "distinct"  # V3
    if (
        author_set_jaccard(a.authors, b.authors) < config.author_set_distinct_jaccard
        and not author_subset(a.authors, b.authors)
    ):
        return "distinct"  # V4
    # Near-identical title + authors but no shared id ⇒ ambiguous (→ SAME_WORK LLM).
    titles_agree = title_ratio(a.title, b.title) >= _SAME_TITLE
    authors_agree = author_overlap(a.authors, b.authors) >= _SAME_AUTHOR or author_subset(
        a.authors, b.authors
    )
    if titles_agree and authors_agree:
        return "ambiguous"
    return "distinct"


def _merge_ids(a: Identifiers, b: Identifiers) -> Identifiers:
    return Identifiers(
        doi=a.doi or b.doi,
        arxiv_id=a.arxiv_id or b.arxiv_id,
        isbn13=a.isbn13 or b.isbn13,
        isbn13s=tuple(dict.fromkeys((*a.isbn13s, *b.isbn13s, *a.all_isbn13(), *b.all_isbn13()))),
        pmid=a.pmid or b.pmid,
        bibcode=a.bibcode or b.bibcode,
        openalex=a.openalex or b.openalex,
        url=a.url or b.url,
    )


def _artifact_from_records(records: list[SourceRecord]) -> MatchedArtifact:
    # Prefer a published (non-preprint), DOI-bearing, highly-cited record as the canonical best.
    best = max(
        records,
        key=lambda r: (0 if r.is_preprint else 1, 1 if r.ids.doi else 0, r.citation_count),
    )
    merged = Identifiers()
    for r in records:
        merged = _merge_ids(merged, r.ids)
    return MatchedArtifact(
        records=list(records), merged_ids=merged, versions=list(records), best_record=best
    )


async def _same_work_llm(
    a: SourceRecord, b: SourceRecord, llm: LLMClient, cache: AuditCache | None
) -> tuple[SameWorkResult | None, bool]:
    user = same_work_user(a, b)
    p_hash = prompt_hash(SAME_WORK_SYSTEM + "\n" + user)
    if cache is not None:
        cached = cache.get_llm_decision(p_hash, "same_work")
        if cached is not None:
            return SameWorkResult.model_validate_json(cached), False
    try:
        result = await llm.structured(SAME_WORK_SYSTEM, user, SameWorkResult, "same_work")
    except LLMError:
        return None, True
    if cache is not None:
        cache.put_llm_decision(p_hash, "same_work", result.model_dump_json())
    return result, False


async def cluster_accepted(
    accepted: list[CandidateAssessment],
    config: AuditConfig,
    llm: LLMClient | None,
    cache: AuditCache | None,
) -> tuple[list[MatchedArtifact], bool]:
    """Cluster accepted candidates into artifacts (union-find). Returns (artifacts, errored)."""
    records = [c.record for c in accepted]
    n = len(records)
    if n <= 1:
        return ([_artifact_from_records(records)] if records else []), False

    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(j)] = find(i)

    errored = False
    for i in range(n):
        for j in range(i + 1, n):
            if find(i) == find(j):
                continue
            rel = formal_relation(records[i], records[j], config)
            if rel == "same":
                union(i, j)
            elif rel == "ambiguous" and llm is not None:
                result, err = await _same_work_llm(records[i], records[j], llm, cache)
                errored = errored or err
                if result and result.relation in ("same_artifact", "versions_of_same_work"):
                    union(i, j)

    clusters: dict[int, list[SourceRecord]] = {}
    for idx, rec in enumerate(records):
        clusters.setdefault(find(idx), []).append(rec)
    return [_artifact_from_records(group) for group in clusters.values()], errored
