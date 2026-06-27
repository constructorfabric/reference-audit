"""LLM adjudication of the `adjudicate` bucket (the danger zone).

Runs one independent `CAN_CORRESPOND` call per surviving candidate (capped, top-k by composite),
caching each decision. A candidate the model affirmatively confirms is promoted to `auto_accept`;
everything else stays as-is. Returns whether an LLM error occurred, so the caller can keep an entry
UNRESOLVED rather than emit a false `none` when the deciding call failed.
"""

from __future__ import annotations

import asyncio

from reference_audit.cache.store import AuditCache, prompt_hash
from reference_audit.config import AuditConfig
from reference_audit.llm.client import LLMClient, LLMError
from reference_audit.llm.prompts import CAN_CORRESPOND_SYSTEM, can_correspond_user
from reference_audit.models import CanCorrespondResult, EntryAudit


async def adjudicate_entry(
    audit: EntryAudit, llm: LLMClient, config: AuditConfig, cache: AuditCache | None
) -> bool:
    """Adjudicate an entry's `adjudicate` candidates with the LLM. Returns errored flag."""
    candidates = [c for c in audit.candidates if c.bucket == "adjudicate"]
    candidates.sort(key=lambda c: c.features.composite, reverse=True)
    candidates = candidates[: config.llm_max_candidates]
    if not candidates:
        return False

    async def decide(candidate) -> tuple[object, CanCorrespondResult | None, bool]:
        system = CAN_CORRESPOND_SYSTEM
        user = can_correspond_user(audit.entry, candidate.record, candidate.features)
        p_hash = prompt_hash(system + "\n" + user)
        if cache is not None:
            cached = cache.get_llm_decision(p_hash, "can_correspond")
            if cached is not None:
                return candidate, CanCorrespondResult.model_validate_json(cached), False
        try:
            result = await llm.structured(
                system, user, CanCorrespondResult, "can_correspond"
            )
        except LLMError:
            return candidate, None, True
        if cache is not None:
            cache.put_llm_decision(p_hash, "can_correspond", result.model_dump_json())
        return candidate, result, False

    outcomes = await asyncio.gather(*(decide(c) for c in candidates))
    errored = False
    for candidate, result, err in outcomes:
        errored = errored or err
        if result is None:
            continue
        candidate.llm = result
        if result.confidence in ("high", "medium"):
            # Confident LLM ruling is decisive: accept → match; reject → confirmed non-match, so the
            # entry can conclude `none` when every candidate is rejected. Low confidence stays
            # `adjudicate` (the entry remains unresolved rather than guessed).
            candidate.bucket = "auto_accept" if result.can_correspond else "auto_reject"
    return errored
