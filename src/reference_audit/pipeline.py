"""Audit pipeline.

M1 ships the parse-only path (`build_parse_report`, no network). Later milestones add the
async `AuditPipeline.run`: parse → generate candidates → score → adjudicate → equivalence →
verdict → report.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tqdm import tqdm

from reference_audit.cache.store import AuditCache
from reference_audit.config import AuditConfig
from reference_audit.llm.client import LLMClient
from reference_audit.matching.adjudicate import adjudicate_entry
from reference_audit.matching.features import compute_features
from reference_audit.matching.pool import pool_candidates
from reference_audit.matching.sameobject import cluster_accepted
from reference_audit.matching.scoring import bucket
from reference_audit.matching.verdict import build_verdict
from reference_audit.models import (
    AuditReport,
    BibEntry,
    CandidateAssessment,
    EntryAudit,
    EntryType,
    SourceQueryResult,
    SourceRecord,
)
from reference_audit.parsing.bib import parse_bib
from reference_audit.parsing.tex import parse_cited_keys
from reference_audit.sources.base import SourceAdapter
from reference_audit.sources.registry import (
    build_default_adapters,
    route_entry,
    venue_allows_no_doi,
)

_NEEDS_ISBN = {EntryType.BOOK, EntryType.INCOLLECTION}
_NEEDS_DOI = {EntryType.ARTICLE, EntryType.INPROCEEDINGS}


class EmptyBibliographyError(ValueError):
    """Raised when the .bib parses to zero auditable entries — nothing to audit."""


def _parse_issues(entry: BibEntry) -> list[str]:
    """Deterministic, no-network issues visible from the .bib alone."""
    issues: list[str] = []
    raw_doi = (entry.raw_fields.get("doi") or "").strip()
    if entry.ids.doi and ("doi.org" in raw_doi.lower() or raw_doi.lower().startswith("http")):
        issues.append(f"DOI normalized from URL form ('{raw_doi}' → '{entry.ids.doi}')")
    if entry.entry_type in _NEEDS_ISBN and not entry.ids.isbn13:
        issues.append("book has no ISBN (will attempt ISBN backfill)")
    if entry.entry_type in _NEEDS_DOI and not entry.ids.doi and not entry.ids.arxiv_id:
        issues.append("no DOI/arXiv id (will attempt DOI backfill)")
    if entry.entry_type == EntryType.MISC and not entry.ids.any_present():
        issues.append("no identifier or URL")
    if not entry.title:
        issues.append("missing title")
    if not entry.authors:
        issues.append("missing author list")
    return issues


# @cpt-flow:cpt-referenceaudit-flow-parsing-build-report:p1
# @cpt-dod:cpt-referenceaudit-dod-parsing-bookkeeping:p1
def build_parse_report(tex_path: str | Path | None, bib_path: str | Path) -> AuditReport:
    """Parse-only report (M1): identifiers + cited/uncited bookkeeping + deterministic issues.

    `tex_path` may be None (audit a .bib without a manuscript); then nothing is 'uncited'.
    """
    # @cpt-begin:cpt-referenceaudit-flow-parsing-build-report:p1:inst-parse-bib
    entries, twins = parse_bib(bib_path)
    if not entries:
        detail = f" ({len(twins)} commented-out twin(s) ignored)" if twins else ""
        raise EmptyBibliographyError(
            f"No auditable bibliography entries found in {bib_path}{detail}. "
            "Is it a valid .bib file, and are the .bib/.tex arguments in the right order?"
        )
    bib_keys = {e.key for e in entries}
    # @cpt-end:cpt-referenceaudit-flow-parsing-build-report:p1:inst-parse-bib

    # @cpt-begin:cpt-referenceaudit-flow-parsing-build-report:p1:inst-parse-tex
    cited_keys: set[str] = set()
    nocite_star = False
    missing_includes: list[str] = []
    if tex_path is not None:
        cited_keys, nocite_star, missing_includes = parse_cited_keys(tex_path)
    # @cpt-end:cpt-referenceaudit-flow-parsing-build-report:p1:inst-parse-tex

    audits: list[EntryAudit] = []
    for e in entries:
        # @cpt-begin:cpt-referenceaudit-flow-parsing-build-report:p1:inst-mark-cited
        e.cited = nocite_star or (e.key in cited_keys)
        # @cpt-end:cpt-referenceaudit-flow-parsing-build-report:p1:inst-mark-cited
        # @cpt-begin:cpt-referenceaudit-flow-parsing-build-report:p1:inst-collect-issues
        audits.append(EntryAudit(entry=e, verdict=None, issues=_parse_issues(e)))
        # @cpt-end:cpt-referenceaudit-flow-parsing-build-report:p1:inst-collect-issues

    cited_but_missing = sorted(k for k in cited_keys if k not in bib_keys)
    uncited = [] if (tex_path is None or nocite_star) else sorted(
        k for k in bib_keys if k not in cited_keys
    )

    by_type: dict[str, int] = {}
    for e in entries:
        by_type[e.entry_type.value] = by_type.get(e.entry_type.value, 0) + 1

    # @cpt-begin:cpt-referenceaudit-flow-parsing-build-report:p1:inst-build-report
    summary = {
        "total_entries": len(entries),
        "cited": sum(1 for e in entries if e.cited),
        "uncited": len(uncited),
        "cited_but_missing": len(cited_but_missing),
        "commented_twins": len(twins),
        "missing_includes": len(missing_includes),
        "entries_with_issues": sum(1 for a in audits if a.issues),
        "by_type": by_type,
    }

    return AuditReport(
        entries=audits,
        cited_but_missing=cited_but_missing,
        uncited=uncited,
        commented_twins=[t.key for t in twins],
        missing_includes=missing_includes,
        summary=summary,
    )
    # @cpt-end:cpt-referenceaudit-flow-parsing-build-report:p1:inst-build-report


def _verdict_summary(report: AuditReport) -> dict[str, int]:
    counts = {"none": 0, "exactly_one": 0, "multiple": 0, "unresolved": 0}
    for a in report.entries:
        counts["unresolved" if a.verdict is None else a.verdict.kind] += 1
    return counts


class AuditPipeline:
    """Async audit pipeline.

    M3: parse → route → (id-lookup + metadata-search, concurrent & cached) → pool candidates by
    identifier/version-edge → score (full feature vector) → 3-way verdict; backfills missing
    DOIs/ISBNs. The LLM funnel over the *adjudicate* bucket (M4) and the full SAME-OBJECT rule +
    LLM tie-break (M5) extend `_audit_entry` / `build_verdict` later.
    """

    def __init__(
        self,
        config: AuditConfig,
        *,
        cache: AuditCache | None = None,
        adapters: list[SourceAdapter] | None = None,
        llm: LLMClient | None = None,
    ):
        self.config = config
        self.cache = cache
        self.adapters: list[SourceAdapter] = (
            adapters if adapters is not None else build_default_adapters(config)
        )
        if llm is not None:
            self.llm: LLMClient | None = llm
        elif config.llm_enabled():
            self.llm = LLMClient(
                model=config.model,
                api_key=config.openai_api_key,
                base_url=config.openai_base_url,
                concurrency=config.llm_concurrency,
            )
        else:
            self.llm = None

    async def aclose(self) -> None:
        for adapter in self.adapters:
            await adapter.aclose()
        if self.llm is not None:
            await self.llm.aclose()

    async def run(
        self, tex_path: str | Path | None, bib_path: str | Path, *, progress: bool = False
    ) -> AuditReport:
        report = build_parse_report(tex_path, bib_path)
        tasks = [asyncio.ensure_future(self._audit_entry(a)) for a in report.entries]
        # Advance a bar as each entry resolves; verdicts land in-place on the audit objects, so
        # completion order is irrelevant. `disable` keeps tests and library callers silent.
        for task in tqdm(
            asyncio.as_completed(tasks),
            total=len(tasks),
            desc="Auditing references",
            unit="ref",
            disable=not progress,
        ):
            await task
        report.summary["verdicts"] = _verdict_summary(report)
        return report

    async def _audit_entry(self, audit: EntryAudit) -> None:
        entry = audit.entry
        if self.cache is not None:
            cached = self.cache.get_entry_verdict(entry.content_hash)
            if cached is not None:
                audit.verdict = cached
                audit.from_cache = True
                return
        route = route_entry(entry, self.adapters)
        if not route.id_adapters and not route.metadata_adapters:
            return  # nothing queryable (e.g. a URL-only @misc — handled in a later milestone)

        records, errored = await self._gather_candidates(entry, route)
        pooled = pool_candidates(records)
        entry_has_id = entry.ids.any_present()
        audit.candidates = [self._assess(entry, r, entry_has_id=entry_has_id) for r in pooled]
        verdict = await self._verdict(audit, errored=errored)

        # README: "unless a returned record is a 100% match, use an LLM to filter results one by
        # one." A formal exactly_one IS the 100%-match short-circuit, so only invoke the per-
        # candidate LLM filter when the formal rules left the entry unresolved.
        if verdict is None and self.llm is not None:
            llm_errored = await adjudicate_entry(audit, self.llm, self.config, self.cache)
            verdict = await self._verdict(audit, errored=errored or llm_errored)

        verdict = self._guard_web_artifact(entry, audit, verdict)
        self._note_backfill(audit, verdict)
        audit.verdict = verdict
        if self.cache is not None and verdict is not None:
            self.cache.put_entry_verdict(entry.content_hash, verdict)

    async def _verdict(self, audit: EntryAudit, *, errored: bool):
        """Cluster the accepted candidates into artifacts (SAME-OBJECT), then count them."""
        accepted = [c for c in audit.candidates if c.bucket == "auto_accept"]
        artifacts, cluster_errored = await cluster_accepted(
            accepted, self.config, self.llm, self.cache
        )
        return build_verdict(audit.candidates, artifacts, errored=errored or cluster_errored)

    async def _gather_candidates(self, entry, route) -> tuple[list[SourceRecord], bool]:
        async def one(adapter: SourceAdapter, kind: str) -> SourceQueryResult:
            cached = (
                self.cache.get_source_query(entry.content_hash, adapter.name, kind)
                if self.cache is not None
                else None
            )
            if cached is not None:
                return cached
            result = (
                await adapter.lookup_by_id(entry.ids)
                if kind == "id"
                else await adapter.search_by_metadata(entry)
            )
            if self.cache is not None:
                self.cache.put_source_query(entry.content_hash, result)
            return result

        tasks = [one(a, "id") for a in route.id_adapters]
        tasks += [one(a, "metadata") for a in route.metadata_adapters]
        results = await asyncio.gather(*tasks)
        records: list[SourceRecord] = []
        errored = False
        for res in results:
            if res.error:
                errored = True
            records.extend(res.records)
        return records, errored

    def _assess(
        self, entry: BibEntry, record: SourceRecord, *, entry_has_id: bool
    ) -> CandidateAssessment:
        features = compute_features(
            entry, record, tail_threshold=self.config.prefix_trap_tail_jaccard
        )
        return CandidateAssessment(
            record=record,
            features=features,
            bucket=bucket(features, self.config, entry_has_id=entry_has_id),
        )

    def _guard_web_artifact(self, entry: BibEntry, audit: EntryAudit, verdict):
        """A URL-only @misc (e.g. a blog/software page) is not a hallucination just because no
        scholarly DB indexes it. Leave it unresolved with a note; URL liveness is a later milestone.
        """
        url_only = (
            entry.entry_type == EntryType.MISC
            and entry.ids.url
            and not (entry.ids.doi or entry.ids.arxiv_id or entry.ids.isbn13)
        )
        if url_only and (verdict is None or verdict.kind == "none"):
            audit.issues.append("URL-only web artifact; liveness check is a later milestone")
            return None
        return verdict

    def _note_backfill(self, audit: EntryAudit, verdict) -> None:
        """Record identifiers discovered for an entry that lacked them (T3 backfill)."""
        if verdict is None or verdict.kind != "exactly_one" or not verdict.artifacts:
            return
        best = verdict.artifacts[0].best_record
        entry = audit.entry
        if best is None:
            return
        if not entry.ids.doi and best.ids.doi:
            audit.issues.append(f"DOI found via {best.source}: {best.ids.doi}")
        if not entry.ids.isbn13 and best.ids.isbn13:
            audit.issues.append(f"ISBN found via {best.source}: {best.ids.isbn13}")
        if (
            not entry.ids.doi
            and not best.ids.doi
            and venue_allows_no_doi(entry.venue)
        ):
            audit.issues.append("no DOI expected for this venue (allowlisted)")


async def _run_async(
    tex_path: str | Path | None,
    bib_path: str | Path,
    config: AuditConfig,
    cache: AuditCache | None,
    progress: bool,
) -> AuditReport:
    pipeline = AuditPipeline(config, cache=cache)
    try:
        return await pipeline.run(tex_path, bib_path, progress=progress)
    finally:
        await pipeline.aclose()


def run_audit(
    tex_path: str | Path | None,
    bib_path: str | Path,
    *,
    config: AuditConfig | None = None,
    cache_path: str | Path | None = None,
    fresh: bool = False,
    progress: bool = False,
) -> AuditReport:
    """Synchronous entry point used by the CLI: build config + cache, run, tear down."""
    config = config or AuditConfig()
    cache: AuditCache | None = None
    if cache_path is not None:
        cache = AuditCache(
            cache_path, pipeline_version=config.pipeline_version, model=config.model
        )
        if fresh:
            cache.clear()
    try:
        return asyncio.run(_run_async(tex_path, bib_path, config, cache, progress))
    finally:
        if cache is not None:
            cache.close()
