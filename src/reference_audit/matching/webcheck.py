"""Web-artifact verification funnel for URL-only ``@misc`` references.

Given the page fetched from the cited URL (`sources.web.WebAdapter`), decide the verdict with a
cheap-first funnel that mirrors the rest of the pipeline:

  1. **Fetch outcome.** A transport error / bot-wall leaves the entry UNRESOLVED (reported, retried —
     never a false 'no match'). A dead link (404/410) is a real finding, also reported, and likewise
     leaves the entry unresolved (a rotted or wrong URL is not, by itself, proof the work never
     existed — reliability: report the gap, never guess a hallucination).
  2. **HTML metadata (deterministic).** If the page's self-declared title matches the cited title
     (and authors corroborate, when both sides list them), the page IS the cited resource →
     ``exactly_one``, with no LLM call (the 100%-match short-circuit).
  3. **LLM fallback.** When the metadata is absent or inconclusive, ask the model whether the page is
     the cited resource (affirmative polarity, cached). A confident yes → ``exactly_one``; a
     high-confidence no → ``none`` (a live page that is positively a different/`not found`/login
     resource — a likely wrong or fabricated URL); anything else stays UNRESOLVED.

Returns ``(verdict, issues)`` — the pipeline owns the audit object. A ``None`` verdict means
unresolved (never cached, retried next run).
"""

from __future__ import annotations

from reference_audit.cache.store import AuditCache, prompt_hash
from reference_audit.config import AuditConfig
from reference_audit.llm.client import LLMClient, LLMError
from reference_audit.llm.prompts import WEB_MATCH_SYSTEM, web_match_user
from reference_audit.matching.features import title_ratio
from reference_audit.matching.names import author_overlap, author_subset
from reference_audit.models import (
    BibEntry,
    Identifiers,
    MatchedArtifact,
    SourceQueryResult,
    SourceRecord,
    Verdict,
    WebMatchResult,
)


def _confirmed(page: SourceRecord, confidence: str, rationale: str) -> Verdict:
    """An ``exactly_one`` verdict whose single artifact is the fetched web page itself."""
    artifact = MatchedArtifact(
        records=[page],
        versions=[page],
        best_record=page,
        merged_ids=page.ids or Identifiers(),
    )
    return Verdict(
        kind="exactly_one",
        artifacts=[artifact],
        confidence=confidence,
        rationale=f"confirmed via web page — {rationale}",
    )


def _metadata_confirms(entry: BibEntry, page: SourceRecord, config: AuditConfig) -> bool:
    """Deterministic step 2: page's self-declared title (and authors, if any) match the citation."""
    if not page.title or title_ratio(entry.title, page.title) < config.web_title_accept:
        return False
    # When both sides list authors they must corroborate; a missing list on either side never blocks
    # (many legitimate pages omit author metadata — that case is what the LLM fallback is for, but a
    # strong title match alone is enough to confirm here).
    if entry.authors and page.authors:
        return (
            author_overlap(entry.authors, page.authors) >= config.author_accept
            or author_subset(entry.authors, page.authors)
        )
    return True


async def _ask_llm(
    entry: BibEntry,
    page: SourceRecord,
    llm: LLMClient,
    cache: AuditCache | None,
) -> WebMatchResult:
    system = WEB_MATCH_SYSTEM
    user = web_match_user(entry, page)
    p_hash = prompt_hash(system + "\n" + user)
    if cache is not None:
        cached = cache.get_llm_decision(p_hash, "web_match")
        if cached is not None:
            return WebMatchResult.model_validate_json(cached)
    result = await llm.structured(system, user, WebMatchResult, "web_match")
    if cache is not None:
        cache.put_llm_decision(p_hash, "web_match", result.model_dump_json())
    return result


async def check_web_reference(
    entry: BibEntry,
    fetched: SourceQueryResult,
    llm: LLMClient | None,
    config: AuditConfig,
    cache: AuditCache | None,
) -> tuple[Verdict | None, list[str]]:
    """Run the web funnel over the fetched page. Returns (verdict | None-for-unresolved, issues)."""
    issues: list[str] = []

    if fetched.error:
        issues.append(
            f"could not check the cited URL (left unresolved, will retry next run): {fetched.error}"
        )
        return None, issues

    page = fetched.records[0] if fetched.records else None
    if page is None:
        issues.append("no URL to check for this web reference")
        return None, issues

    raw = page.raw or {}
    if raw.get("dead"):
        issues.append(
            f"cited URL is a dead link (HTTP {raw.get('status')} — page no longer exists); "
            "the reference could not be verified"
        )
        return None, issues

    # Step 2 — deterministic HTML-metadata check (no LLM cost on a clean match). A clean confirm is
    # NOT a problem: it is reported on the verdict line (rationale) only, never appended to `issues`
    # — `issues` drives the report's needs-attention grouping + ⚠, so a success there is misleading.
    if _metadata_confirms(entry, page, config):
        author_note = " + author" if (entry.authors and page.authors) else ""
        return _confirmed(page, "high", f"page HTML metadata matches the citation (title{author_note})"), issues

    # Step 3 — metadata absent or inconclusive → LLM.
    if llm is None:
        issues.append(
            "cited URL is live but its HTML metadata did not confirm the reference; "
            "LLM check is disabled (--no-llm), so it is left unresolved"
        )
        return None, issues

    try:
        decision = await _ask_llm(entry, page, llm, cache)
    except LLMError:
        issues.append(
            "cited URL is live but could not be confirmed: the LLM check failed "
            "(left unresolved, will retry next run)"
        )
        return None, issues

    if decision.corresponds and decision.confidence in ("high", "medium"):
        # As with the metadata confirm: a success is carried by the verdict rationale, not `issues`.
        return _confirmed(page, decision.confidence, f"LLM-verified ({decision.reason})"), issues

    if not decision.corresponds and decision.confidence == "high":
        # Positive evidence the live page is a *different* resource (a wrong/fabricated URL): the
        # README's `none`. A weaker rejection is not enough to cry hallucination — it stays unresolved.
        issues.append(
            "the cited URL is live but its content does NOT correspond to the reference "
            f"(LLM-judged: {decision.reason}) — possible wrong or fabricated URL"
        )
        return (
            Verdict(
                kind="none",
                confidence="high",
                rationale=f"the live page at the cited URL is not the cited resource ({decision.reason})",
            ),
            issues,
        )

    issues.append(
        "cited URL is live but could not be confirmed as the reference "
        f"(LLM uncertain: {decision.reason}) — left unresolved"
    )
    return None, issues
