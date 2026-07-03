"""Citation alignment: is a reference cited for a claim its source actually makes?

Once step 1 has matched an entry to exactly one real work, the work's *identity* is settled — but it
may still be cited for something the work does not claim. This module pairs each citing context (the
sentence(s) that invoke the reference, from `parsing/tex.py`) with the cited work's ABSTRACT and
classifies the usage:

  - ``supported``       the abstract corroborates the citing claim.
  - ``contradicted``    the abstract asserts the opposite — the one "loud" finding.
  - ``not_in_abstract`` the abstract is silent on the claim. An abstract is only a summary, so its
                        silence is NOT misuse — the full text may well support the claim.
  - ``unverifiable``    no abstract was retrievable, the LLM is unavailable, or the check failed.

Per the reliability contract, a silent/absent abstract or ANY failure yields
``not_in_abstract``/``unverifiable`` — NEVER ``contradicted``. Each citation is judged in isolation
(one failure never affects the others), and the check is advisory: it never changes the step-1
verdict. LLM decisions are cached by ``(prompt, kind, model)``.
"""

from __future__ import annotations

import asyncio

from reference_audit.cache.store import AuditCache, prompt_hash
from reference_audit.config import AuditConfig
from reference_audit.llm.client import LLMClient, LLMError
from reference_audit.llm.prompts import CITATION_ALIGNMENT_SYSTEM, citation_alignment_user
from reference_audit.models import (
    AlignmentFinding,
    BibEntry,
    CitationAlignmentResult,
    CitationContext,
    MatchedArtifact,
)

_KIND = "citation_alignment"


def artifact_abstract(artifact: MatchedArtifact) -> str:
    """The fullest abstract available anywhere on the matched artifact.

    Scans every record/version (and the best record) rather than trusting one representative, so an
    abstract that rode in on any merged source is found. Empty string when none carries one — which
    the caller reports as ``unverifiable``, never a guess.
    """
    records = [*artifact.records, *artifact.versions]
    if artifact.best_record is not None:
        records.append(artifact.best_record)
    return max(((r.abstract or "").strip() for r in records), key=len, default="")


def _unverifiable(ctx: CitationContext, detail: str) -> AlignmentFinding:
    return AlignmentFinding(
        key=ctx.key, context_text=ctx.text, ordinal=ctx.ordinal, status="unverifiable", detail=detail
    )


def _apply(ctx: CitationContext, result: CitationAlignmentResult) -> AlignmentFinding:
    return AlignmentFinding(
        key=ctx.key,
        context_text=ctx.text,
        ordinal=ctx.ordinal,
        status=result.classification,
        evidence_quote=result.evidence_quote,
        detail=result.reason,
        confidence=result.confidence,
        via_llm=True,
    )


async def _classify_one(
    entry: BibEntry,
    ctx: CitationContext,
    abstract: str,
    llm: LLMClient | None,
    cache: AuditCache | None,
) -> AlignmentFinding:
    """Classify one citing context. Never raises — any failure becomes ``unverifiable`` so a single
    citation's problem cannot abort the entry or the other citations (per-citation isolation)."""
    if not abstract:
        return _unverifiable(ctx, "no abstract available for the cited work; alignment not checked")
    if llm is None:
        return _unverifiable(ctx, "LLM unavailable; citation alignment not checked")
    user = citation_alignment_user(entry, ctx.text, abstract)
    p_hash = prompt_hash(CITATION_ALIGNMENT_SYSTEM + "\n" + user)
    if cache is not None:
        cached = cache.get_llm_decision(p_hash, _KIND)
        if cached is not None:
            return _apply(ctx, CitationAlignmentResult.model_validate_json(cached))
    try:
        result = await llm.structured(
            CITATION_ALIGNMENT_SYSTEM, user, CitationAlignmentResult, _KIND
        )
    except asyncio.CancelledError:
        raise
    except LLMError as exc:
        return _unverifiable(ctx, f"citation alignment check failed ({exc})")
    except Exception as exc:  # noqa: BLE001 — advisory; isolate this citation, never abort the entry
        return _unverifiable(ctx, f"citation alignment check errored ({exc})")
    if cache is not None:
        cache.put_llm_decision(p_hash, _KIND, result.model_dump_json())
    return _apply(ctx, result)


async def resolve_alignment_findings(
    entry: BibEntry,
    artifact: MatchedArtifact,
    contexts: list[CitationContext],
    llm: LLMClient | None,
    config: AuditConfig,  # noqa: ARG001 — symmetry with resolve_field_findings; reserved
    cache: AuditCache | None,
) -> list[AlignmentFinding]:
    """Alignment findings for every citing context of one matched entry (concurrent, isolated)."""
    if not contexts:
        return []
    abstract = artifact_abstract(artifact)
    return list(
        await asyncio.gather(*(_classify_one(entry, c, abstract, llm, cache) for c in contexts))
    )


def alignment_note(f: AlignmentFinding) -> str:
    """One-line human summary for the text report (only actionable findings are surfaced)."""
    where = f" [cite #{f.ordinal + 1}]" if f.ordinal else ""
    if f.status == "contradicted":
        quote = f" — abstract says: “{f.evidence_quote}”" if f.evidence_quote else ""
        return (
            f"citation{where} may misrepresent the source: the cited abstract appears to contradict "
            f"“{f.context_text}” — {f.detail}{quote}"
        )
    if f.status == "not_in_abstract":
        return (
            f"citation{where} not confirmed by the abstract (the abstract is silent on "
            f"“{f.context_text}”; full text may support it) — {f.detail}"
        )
    return f"citation{where} alignment could not be verified — {f.detail}"
