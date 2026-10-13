"""Check resolved paper IDs against citation fields before metadata can choose another work."""

from dataclasses import dataclass, field

from reference_audit.cache.store import AuditCache
from reference_audit.config import AuditConfig
from reference_audit.fieldcheck import _is_placeholder, resolve_field_findings
from reference_audit.llm.client import LLMClient
from reference_audit.models import EntryAudit, EntryType, MatchedArtifact, SourceRecord, Verdict
from reference_audit.matching.pool import pool_candidates


PAPER_ID_KINDS = ("doi", "arxiv_id", "pmid", "bibcode", "openalex")


@dataclass
class IDConsistencyResult:
    verdict: Verdict | None = None
    incomplete_reasons: list[str] = field(default_factory=list)


# @cpt-algo:cpt-referenceaudit-algo-identification-contradiction:p1
async def check_id_contradiction(
    audit: EntryAudit,
    id_records: list[SourceRecord],
    all_records: list[SourceRecord],
    config: AuditConfig,
    llm: LLMClient | None,
    cache: AuditCache | None,
) -> IDConsistencyResult:
    # @cpt-begin:cpt-referenceaudit-algo-identification-contradiction:p1:inst-targets
    entry = audit.entry
    if entry.entry_type in (EntryType.BOOK, EntryType.INCOLLECTION):
        return IDConsistencyResult()  # books/chapters use the edition-aware book path
    targets = []
    pooled = pool_candidates(all_records)
    for kind in PAPER_ID_KINDS:
        value = getattr(entry.ids, kind)
        if not value:
            continue
        resolved = [r for r in id_records if getattr(r.ids, kind) == value]
        if resolved:
            # Related versions may supply the cited published/preprint metadata. An exact
            # successful ID lookup is still required; metadata hits cannot establish resolution.
            records = [m for group in pooled
                       if any(getattr(m.ids, kind) == value for m in (group.members or [group]))
                       for m in (group.members or [group])]
            targets.append((kind, value, resolved[0], records))
    # @cpt-end:cpt-referenceaudit-algo-identification-contradiction:p1:inst-targets

    # @cpt-begin:cpt-referenceaudit-algo-identification-contradiction:p1:inst-fields
    checked = []
    incomplete = []
    for kind, value, best, records in targets:
        # Keep the raw resolved target as best_record; field checks choose its cited version.
        artifact = MatchedArtifact(records=records, versions=records, best_record=best,
                                   merged_ids=best.ids)
        # An omitted co-author is incomplete metadata, not evidence of a different ID target.
        # Keep this identity decision independent of the optional partial-author warning policy.
        identity_config = config.model_copy(update={"partial_authors": "ignore"})
        findings = await resolve_field_findings(entry, artifact, llm, identity_config, cache)
        errors = [f for f in findings if f.status == "error"
                  and not _is_placeholder(f.bib_value) and f.canonical_value.strip()]
        checked.append((kind, value, artifact, findings, errors))
        for f in findings:
            if f.status == "uncertain" and (
                f.via_llm or "LLM unavailable" in f.detail or "LLM check failed" in f.detail
            ):
                incomplete.append(
                    f"hard ID consistency check incomplete for {kind}:{value}, {f.field}: {f.detail}"
                )
    # @cpt-end:cpt-referenceaudit-algo-identification-contradiction:p1:inst-fields

    # @cpt-begin:cpt-referenceaudit-algo-identification-contradiction:p1:inst-result
    for kind, value, artifact, findings, errors in checked:
        if errors:
            target = artifact.best_record
            details = "; ".join(f"{f.field}: {f.detail}" for f in errors)
            verdict = Verdict(
                kind="contradiction", artifacts=[artifact], contradiction_findings=findings,
                confidence="medium" if all(f.via_llm for f in errors) else "high",
                rationale=f"Hard ID {kind}:{value} resolves to '{target.title}' "
                          f"({target.source}), but the citation contradicts its metadata: {details}",
            )
            # One definitive conflict settles this verdict. Other uncertain fields remain
            # explicit in contradiction_findings but cannot turn the proven conflict into a match.
            return IDConsistencyResult(verdict)
    return IDConsistencyResult(incomplete_reasons=incomplete)
    # @cpt-end:cpt-referenceaudit-algo-identification-contradiction:p1:inst-result
