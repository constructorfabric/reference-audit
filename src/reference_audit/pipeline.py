"""Audit pipeline.

M1 ships the parse-only path (`build_parse_report`, no network). Later milestones add the
async `AuditPipeline.run`: parse → generate candidates → score → adjudicate → equivalence →
verdict → report.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from tqdm import tqdm

from reference_audit.cache.store import AuditCache
from reference_audit.config import AuditConfig
from reference_audit.llm.client import LLMClient
from reference_audit.matching.adjudicate import adjudicate_entry
from reference_audit.matching.features import _published_doi, compute_features
from reference_audit.matching.names import mismatched_authors
from reference_audit.matching.pool import pool_candidates
from reference_audit.matching.sameobject import cluster_accepted
from reference_audit.matching.scoring import bucket
from reference_audit.matching.verdict import build_verdict
from reference_audit.matching.webcheck import check_web_reference
from reference_audit.models import (
    AuditReport,
    BibEntry,
    CandidateAssessment,
    EntryAudit,
    EntryType,
    Identifiers,
    MatchedArtifact,
    SourceQueryResult,
    SourceRecord,
    Verdict,
)
from reference_audit.bookcheck import (
    better_edition_note,
    describe_cited_edition,
    latest_edition,
    match_cited_edition,
)
from reference_audit.fieldcheck import (
    check_book_edition_fields,
    consulted_sources,
    finding_note,
    resolve_field_findings,
)
from reference_audit.alignmentcheck import alignment_note, resolve_alignment_findings
from reference_audit.parsing.bib import parse_bib
from reference_audit.parsing.tex import parse_cited_keys, parse_citation_contexts
from reference_audit.sources.base import SourceAdapter
from reference_audit.versioning import better_version_notes
from reference_audit.sources.registry import (
    build_default_adapters,
    route_entry,
    venue_allows_no_doi,
)

_NEEDS_ISBN = {EntryType.BOOK, EntryType.INCOLLECTION}
_NEEDS_DOI = {EntryType.ARTICLE, EntryType.INPROCEEDINGS}


@dataclass
class _BookResolution:
    """Outcome of consulting Open Library (the authority of record for books) for one entry.

    `error` set ⇒ Open Library could not be reached (reported, never read as 'no editions').
    `matched` is the cited edition (confirms identity + sources year/publisher); `latest` is the most
    recent edition (the better-version target).
    """

    error: str | None = None
    editions: list[SourceRecord] = field(default_factory=list)
    matched: SourceRecord | None = None
    latest: SourceRecord | None = None
    # Set when the entry carried no ISBN of its own and we located the book via an ISBN backfilled
    # from the cited DOI's record (a chapter-level DOI of the book). Drives the explanatory note.
    backfilled_isbn: str | None = None


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


def _shares_strong_id(a: Identifiers, b: Identifiers) -> bool:
    """True if two identifier sets agree on any strong identifier (DOI / ISBN13 / arXiv)."""
    return bool(
        (a.doi and a.doi == b.doi)
        or bool(a.all_isbn13() & b.all_isbn13())
        or (a.arxiv_id and a.arxiv_id == b.arxiv_id)
    )


def _verdict_records(verdict: Verdict | None) -> list[SourceRecord]:
    """Flatten the records behind a verdict's artifacts (used to re-derive book findings from a
    cached verdict — the records carry the ISBN that originally located the book)."""
    if verdict is None:
        return []
    return [r for a in verdict.artifacts for r in a.records]


def _is_url_only_web(entry: BibEntry) -> bool:
    """A web @misc identified ONLY by a URL (no DOI/arXiv/ISBN) — the `web.py` check's domain."""
    return bool(
        entry.entry_type == EntryType.MISC
        and entry.ids.url
        and not (entry.ids.doi or entry.ids.arxiv_id or entry.ids.isbn13)
    )


def _web_fetch_predates_render(cached: SourceQueryResult) -> bool:
    """True for a web fetch cached *before* SPA rendering existed. The source-query cache is not
    versioned by pipeline_version, so an old cached shell would otherwise be served and skip the new
    render path. Every live (non-dead) web record now carries ``raw['render']``; its absence marks a
    pre-feature fetch that must be re-fetched once so an SPA shell actually gets rendered."""
    for rec in cached.records:
        raw = rec.raw or {}
        if not raw.get("dead") and "render" not in raw:
            return True
    return False


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
        # Per-key citing contexts for the alignment check, populated by `run` when enabled.
        self._citation_contexts: dict[str, list[CitationContext]] = {}

    async def aclose(self) -> None:
        for adapter in self.adapters:
            await adapter.aclose()
        if self.llm is not None:
            await self.llm.aclose()

    async def run(
        self, tex_path: str | Path | None, bib_path: str | Path, *, progress: bool = False
    ) -> AuditReport:
        report = build_parse_report(tex_path, bib_path)
        # Citation-alignment (opt-in) needs the citing context from the .tex; extract it once, offline,
        # up front. Without a manuscript there is no context, so the check simply produces nothing.
        if self.config.check_alignment and tex_path is not None:
            self._citation_contexts = parse_citation_contexts(tex_path)
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
        # Isolation: a failure auditing one reference must never abort the run (or cancel the
        # other in-flight entries). Anything unexpected leaves this entry unresolved — an error is
        # not a 'not found', so it is retried on the next run rather than reported as a miss.
        try:
            await self._audit_entry_inner(audit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            audit.verdict = None
            audit.issues.append(f"audit failed (left unresolved, will retry next run): {exc}")

    # @cpt-flow:cpt-referenceaudit-flow-identification-audit-entry:p1
    # @cpt-dod:cpt-referenceaudit-dod-identification-identify-artifact:p1
    async def _audit_entry_inner(self, audit: EntryAudit) -> None:
        entry = audit.entry
        if self.cache is not None:
            # @cpt-begin:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-cache-lookup
            cached = self.cache.get_entry_verdict(entry.content_hash)
            # @cpt-end:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-cache-lookup
            if cached is not None:
                audit.verdict = cached
                audit.from_cache = True
                # Issues/field findings aren't part of the cached verdict; recompute them from the
                # cached artifact records (all deterministic, and per-field LLM decisions are
                # themselves cached) so a cached run reports identically to a --fresh one.
                await self._enrich_artifact(audit, cached)
                await self._note_backfill(audit, cached)
                self._note_better_version(audit, cached)
                await self._check_fields(audit, cached)
                # Web @misc: re-derive the page-check issues (the fetch + any LLM decision are cached,
                # so this re-runs against the caches and reproduces the same verdict it discards).
                await self._resolve_web(audit, cached)
                # Books: re-derive the Open Library edition findings (editions are cached, so this is
                # the same single fetch a --fresh run made) and report them identically. The cached
                # artifact's records carry the ISBN (incl. one backfilled from a chapter DOI), so the
                # book is re-located the same way and the report can't contradict the cached verdict.
                await self._report_book(
                    audit, await self._resolve_book(audit, _verdict_records(cached))
                )
                # Citation alignment: re-derive from the cached artifact's abstract + cached per-
                # context LLM decisions, so a cached run reports identically to a --fresh one.
                await self._check_alignment(audit, cached)
                return
        # @cpt-begin:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-route
        route = route_entry(entry, self.adapters)
        # @cpt-end:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-route
        if not route.id_adapters and not route.metadata_adapters:
            # No scholarly source routed (the default set always routes @misc to the aggregators, so
            # this is a reduced-adapter configuration). A URL-only web @misc is still verifiable
            # against its own page; anything else is simply left unresolved.
            verdict = await self._resolve_web(audit, None)
            audit.verdict = verdict
            if self.cache is not None and verdict is not None:
                self.cache.put_entry_verdict(entry.content_hash, verdict)
            return

        # @cpt-begin:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-gather
        records, errored = await self._gather_candidates(entry, route)
        # @cpt-end:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-gather
        # @cpt-begin:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-assess
        pooled = pool_candidates(records)
        # A bare URL is not a matching anchor (no feature compares it), so a conference paper cited
        # only by its proceedings URL is, for scoring, as anchorless as one with no id — eligible for
        # the strict title+author backfill path. This is what lets a DBLP-confirmed NeurIPS/ICLR/ICML
        # paper (no DOI) reach a deterministic verdict instead of depending on the LLM.
        entry_has_id = entry.ids.has_strong_id()
        audit.candidates = [self._assess(entry, r, entry_has_id=entry_has_id) for r in pooled]
        # @cpt-end:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-assess
        # @cpt-begin:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-verdict
        verdict = await self._verdict(audit, errored=errored)
        # @cpt-end:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-verdict

        # README: "unless a returned record is a 100% match, use an LLM to filter results one by
        # one." A formal exactly_one IS the 100%-match short-circuit, so only invoke the per-
        # candidate LLM filter when the formal rules left the entry unresolved.
        # @cpt-begin:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-llm-adjudicate
        if verdict is None and self.llm is not None:
            llm_errored = await adjudicate_entry(audit, self.llm, self.config, self.cache)
            verdict = await self._verdict(audit, errored=errored or llm_errored)
        # @cpt-end:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-llm-adjudicate

        # URL-only web @misc (a blog/software page no scholarly DB indexes): fetch the cited page and
        # verify it via its HTML metadata, then an LLM fallback. Runs only when the DB path found
        # nothing real, so it never overrides a genuine scholarly match.
        # @cpt-begin:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-web
        verdict = await self._resolve_web(audit, verdict)
        # @cpt-end:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-web

        # OpenAlex Work id: a cited openalex.org Work URL is authoritative identity (the author
        # pointed us at the exact Work). When the by-id lookup returns that Work and it matches the
        # entry's title+author, pin it — never let the article-centric pooler dissolve it into a
        # similar-titled foreign-DOI record and backfill the wrong identifiers.
        verdict = self._apply_openalex_identity(verdict, entry, records)

        # Google Books volume id: a cited books.google.…/books?id=… is likewise author-supplied,
        # authoritative identity for a trade/book title. Pin it when the by-id lookup confirms it, so
        # a same-titled journal-article (e.g. a book *review* reusing the book's title+authors, which
        # carries a DOI the book lacks) can't be matched and backfill its wrong DOI onto the book.
        verdict = self._apply_google_books_identity(verdict, entry, records)

        # Books: Open Library is the authority of record for identity. It is edition-aware, so it can
        # confirm a real book the article-centric matcher rejected (a 1976 original whose only
        # DOI-bearing candidate is a 2018 reprint — a 42-year/ISBN gap that score-rejects). The
        # confirmed cited edition becomes the matched artifact, settled BEFORE caching.
        # @cpt-begin:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-book
        book = await self._resolve_book(audit, records)
        verdict = self._apply_book_identity(verdict, book)
        # @cpt-end:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-book

        # @cpt-begin:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-best-output
        await self._note_backfill(audit, verdict)
        self._note_better_version(audit, verdict)
        # Enrich BEFORE caching the verdict, so the cached artifact already carries the authoritative
        # by-id / publisher-of-record records (identity is settled; this only improves field sourcing).
        await self._enrich_artifact(audit, verdict)
        # @cpt-end:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-best-output
        # @cpt-begin:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-cache-store
        audit.verdict = verdict
        if self.cache is not None and verdict is not None:
            self.cache.put_entry_verdict(entry.content_hash, verdict)
        # @cpt-end:cpt-referenceaudit-flow-identification-audit-entry:p1:inst-cache-store
        await self._check_fields(audit, verdict)
        await self._report_book(audit, book)
        await self._check_alignment(audit, verdict)

    # @cpt-algo:cpt-referenceaudit-algo-identification-verdict:p1
    async def _verdict(self, audit: EntryAudit, *, errored: bool):
        """Cluster the accepted candidates into artifacts (SAME-OBJECT), then count them."""
        # @cpt-begin:cpt-referenceaudit-algo-identification-verdict:p1:inst-cluster
        accepted = [c for c in audit.candidates if c.bucket == "auto_accept"]
        artifacts, cluster_errored = await cluster_accepted(
            accepted, self.config, self.llm, self.cache
        )
        # @cpt-end:cpt-referenceaudit-algo-identification-verdict:p1:inst-cluster
        # @cpt-begin:cpt-referenceaudit-algo-identification-verdict:p1:inst-build
        return build_verdict(audit.candidates, artifacts, errored=errored or cluster_errored)
        # @cpt-end:cpt-referenceaudit-algo-identification-verdict:p1:inst-build

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

    async def _gather_by_id(
        self, entry: BibEntry, ids: Identifiers, adapters: list[SourceAdapter]
    ) -> tuple[list[SourceRecord], list[str]]:
        """Query the given adapters by `ids` (cached per entry). Returns (records, error notes)."""

        async def one(adapter: SourceAdapter) -> SourceQueryResult:
            cached = (
                self.cache.get_source_query(entry.content_hash, adapter.name, "id")
                if self.cache is not None
                else None
            )
            if cached is not None:
                return cached
            result = await adapter.lookup_by_id(ids)
            if self.cache is not None:
                self.cache.put_source_query(entry.content_hash, result)
            return result

        results = await asyncio.gather(*(one(a) for a in adapters))
        records: list[SourceRecord] = []
        errors: list[str] = []
        for res in results:
            records.extend(res.records)
            if res.error:
                errors.append(res.error)
        return records, errors

    async def _enrich_artifact(self, audit: EntryAudit, verdict) -> None:
        """Step 2.5 (advisory): enrich an exactly-one artifact with authoritative by-id records — the
        publisher-of-record citation export and any DOI-keyed registration records the initial
        metadata search missed — before field checking.

        Identity is already settled (exactly_one), so enrichment only improves *field canonicalization*
        and never changes the verdict. A discovered identifier (T3 backfill) is the most authoritative
        key we will ever have for the entry; here it is finally used to query, not merely reported. A
        blocked authority of record is surfaced (reliability: report the gap, never guess).
        """
        if not self.config.check_fields:
            return
        if verdict is None or verdict.kind != "exactly_one" or not verdict.artifacts:
            return
        if audit.entry.entry_type in _NEEDS_ISBN:
            # Books are identity-resolved by Open Library to a SPECIFIC edition; re-pooling by the
            # work's ISBN would dissolve that edition back into the work and lose its year/publisher.
            return
        artifact = verdict.artifacts[0]
        ids = artifact.merged_ids
        if not (ids.doi or ids.isbn13):
            return  # no strong identifier to enrich by
        probe = BibEntry(
            key=audit.entry.key, entry_type=audit.entry.entry_type, title=audit.entry.title, ids=ids
        )
        # Authoritative by-id sources for the (now-known) identifier, plus the publisher of record —
        # which lives only here, never in identity routing (a blocked publisher must not affect the
        # verdict).
        id_adapters = list(route_entry(probe, self.adapters).id_adapters)
        publisher = next((a for a in self.adapters if a.name == "publisher"), None)
        if publisher is not None and ids.doi and publisher not in id_adapters:
            id_adapters.append(publisher)
        if not id_adapters:
            return
        try:
            new_records, errors = await self._gather_by_id(audit.entry, ids, id_adapters)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — advisory; report, never fail the entry
            audit.issues.append(f"metadata enrichment skipped (verdict unaffected): {exc}")
            return
        for err in errors:
            audit.issues.append(f"authority of record not auto-retrievable (field check may be incomplete) — {err}")
        if not new_records:
            return
        pooled = pool_candidates(list(artifact.records) + new_records)
        # Identity is fixed; keep the representative(s) that still carry the matched identifier.
        matched = [r for r in pooled if _shares_strong_id(r.ids, ids)] or pooled
        artifact.records = matched
        artifact.versions = matched
        artifact.best_record = max(
            matched,
            key=lambda r: (0 if r.is_preprint else 1, 1 if r.ids.doi else 0, r.citation_count),
        )

    # @cpt-algo:cpt-referenceaudit-algo-identification-score:p1
    def _assess(
        self, entry: BibEntry, record: SourceRecord, *, entry_has_id: bool
    ) -> CandidateAssessment:
        # @cpt-begin:cpt-referenceaudit-algo-identification-score:p1:inst-features
        features = compute_features(
            entry, record, tail_threshold=self.config.prefix_trap_tail_jaccard
        )
        # @cpt-end:cpt-referenceaudit-algo-identification-score:p1:inst-features
        # @cpt-begin:cpt-referenceaudit-algo-identification-score:p1:inst-bucket
        return CandidateAssessment(
            record=record,
            features=features,
            bucket=bucket(features, self.config, entry_has_id=entry_has_id),
        )
        # @cpt-end:cpt-referenceaudit-algo-identification-score:p1:inst-bucket

    async def _resolve_web(self, audit: EntryAudit, verdict):
        """Verify a URL-only @misc by fetching its cited page (HTML metadata, then an LLM fallback).

        A URL-only web artifact (a blog/software page) is not a hallucination just because no
        scholarly DB indexes it, so this steps in precisely when the DB path found nothing real
        (verdict None or `none`). A genuine scholarly match is left untouched. Idempotent over the
        caches (fetch + any LLM decision are cached), so the cached fast path re-runs it to re-derive
        the same issues; see `check_web_reference` for the funnel and the verdict semantics.
        """
        entry = audit.entry
        if not _is_url_only_web(entry):
            return verdict
        # A real scholarly match (or ambiguity) stands; only own the None/`none` outcome — except a
        # prior web match, which we re-derive (the cached fast path passes the cached web verdict in).
        if verdict is not None and verdict.kind in ("exactly_one", "multiple"):
            best = verdict.artifacts[0].best_record if verdict.artifacts else None
            if not (best and best.source == "web"):
                return verdict
        web = next((a for a in self.adapters if a.name == "web"), None)
        if web is None:
            audit.issues.append("URL-only web reference not checked (web adapter not configured)")
            return None
        fetched = await self._fetch_web(entry, web)
        web_verdict, issues = await check_web_reference(
            entry, fetched, self.llm, self.config, self.cache
        )
        audit.issues.extend(issues)
        return web_verdict

    async def _fetch_web(self, entry: BibEntry, web: SourceAdapter) -> SourceQueryResult:
        """Fetch the entry's cited URL (cached per entry, like any source query)."""
        if self.cache is not None:
            cached = self.cache.get_source_query(entry.content_hash, web.name, "web")
            if cached is not None and not _web_fetch_predates_render(cached):
                return cached
        result = await web.fetch_page(entry.ids.url)
        if self.cache is not None:
            self.cache.put_source_query(entry.content_hash, result)
        return result

    async def _note_backfill(self, audit: EntryAudit, verdict) -> None:
        """Record identifiers discovered for an entry that lacked them (T3 backfill)."""
        if verdict is None or verdict.kind != "exactly_one" or not verdict.artifacts:
            return
        best = verdict.artifacts[0].best_record
        entry = audit.entry
        if best is None:
            return
        if not entry.ids.doi and best.ids.doi:
            audit.issues.append(await self._backfilled_doi_note(best.ids.doi, best.source))
        if not entry.ids.isbn13 and best.ids.isbn13:
            audit.issues.append(f"ISBN found via {best.source}: {best.ids.isbn13}")
        if (
            not entry.ids.doi
            and not best.ids.doi
            and venue_allows_no_doi(entry.venue)
        ):
            audit.issues.append("no DOI expected for this venue (allowlisted)")
        if best.authors:
            for wrong in mismatched_authors(entry.authors, best.authors):
                audit.issues.append(
                    f"author '{wrong}' not found in {best.source} record (possible wrong name)"
                )

    async def _backfilled_doi_note(self, doi: str, source: str) -> str:
        """Backfill message for a *discovered* DOI, gated on whether it actually resolves.

        A discovered DOI is the most authoritative key we report for the entry, so it must be real.
        An unregistered one (doi.org 404 — commonly an author-supplied placeholder echoed from
        preprint metadata, the origin of this entry's `10.5555/...`) is reported as a defect, never
        presented as a clean find. With no resolver available we fall back to trusting the source
        (prior behaviour); a doi.org outage leaves the find 'unconfirmed' — reported, never asserted
        valid or invalid.
        """
        publisher = next((a for a in self.adapters if a.name == "publisher"), None)
        if publisher is None:
            return f"DOI found via {source}: {doi}"
        resolves = await self._doi_resolves(doi, publisher)
        if resolves is False:
            return (
                f"DOI found via {source} ({doi}) does NOT resolve at doi.org "
                "(404 — DOI Not Found): likely an invalid identifier (e.g. author-supplied in "
                "preprint/arXiv metadata). The work was matched, but this DOI is unusable."
            )
        if resolves is None:
            return (
                f"DOI found via {source}: {doi} "
                "(could not confirm it resolves at doi.org — verify before use)"
            )
        return f"DOI found via {source}: {doi}"

    async def _doi_resolves(self, doi: str, publisher: SourceAdapter) -> bool | None:
        """doi.org's verdict on a DOI (cached): True=registered, False=404, None=undetermined.

        Only definitive True/False are cached; an undetermined outcome (outage) must retry, mirroring
        the never-cache-errors invariant.
        """
        if self.cache is not None:
            cached = self.cache.get_doi_resolution(doi)
            if cached is not None:
                return cached
        try:
            resolves = await publisher.doi_registered(doi)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — advisory; a failed check is undetermined, never invalid
            return None
        if resolves is not None and self.cache is not None:
            self.cache.put_doi_resolution(doi, resolves)
        return resolves

    # @cpt-dod:cpt-referenceaudit-dod-identification-best-version:p1
    def _note_better_version(self, audit: EntryAudit, verdict) -> None:
        """Step 2: report if a better version of the matched artifact is available.

        Books are excluded here — their better-edition advice is edition-grounded in `_check_book`.
        """
        if audit.entry.entry_type in _NEEDS_ISBN:
            return
        if verdict is None or verdict.kind != "exactly_one" or not verdict.artifacts:
            return
        for note in better_version_notes(audit.entry, verdict.artifacts[0]):
            audit.issues.append(note)

    async def _check_fields(self, audit: EntryAudit, verdict) -> None:
        """Step 3: verify each field of an exactly-one match against the canonical record.

        Advisory only — never alters the verdict. Errors / could-not-verify findings are surfaced
        as issues; the full per-field result (including benign formatting variants) stays on
        `audit.field_findings` for machine consumers.
        """
        if not self.config.check_fields:
            return
        if verdict is None or verdict.kind != "exactly_one" or not verdict.artifacts:
            return
        # A URL-only web @misc is confirmed against the page itself (`_resolve_web`); the page's
        # self-declared og/citation metadata is not an authoritative bibliographic record, so running
        # the article field check against it only yields spurious "could not verify" noise. Skip it.
        if _is_url_only_web(audit.entry):
            return
        # Advisory step: a field-check failure must never clear the (already-decided) verdict or
        # abort the entry, so isolate it here rather than letting it reach `_audit_entry`.
        # Books delegate year/publisher to the edition-grounded `_check_book`, so skip them here
        # (comparing an original edition against a pooled reprint produced false positives).
        skip_fields = (
            frozenset({"year", "publisher"})
            if audit.entry.entry_type in _NEEDS_ISBN
            else frozenset()
        )
        try:
            findings = await resolve_field_findings(
                audit.entry, verdict.artifacts[0], self.llm, self.config, self.cache,
                skip_fields=skip_fields,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — advisory; report, don't fail the entry
            audit.issues.append(f"field correctness check failed (verdict unaffected): {exc}")
            return
        audit.field_findings = findings
        for f in findings:
            if f.status in ("error", "uncertain"):
                audit.issues.append(finding_note(f))
        # Collapse the (often several) could-not-verify fields into a single line to keep the report
        # readable while still reporting the gap (reliability: never silently pass an unchecked field).
        unverifiable = [f.field for f in findings if f.status == "unverifiable"]
        if unverifiable:
            # Name the sources we actually consulted — never claim universal absence (a null field on
            # the sources we reached is not proof the datum does not exist; cf. the publisher export).
            where = ", ".join(consulted_sources(verdict.artifacts[0])) or "any source we could reach"
            audit.issues.append(
                f"could not verify field(s) {', '.join(unverifiable)} — "
                f"not present in the metadata from {where}"
            )

    async def _check_alignment(self, audit: EntryAudit, verdict) -> None:
        """Citation alignment: check each citing context against the cited work's abstract.

        Advisory only — never alters the verdict. Runs on an `exactly_one` match when enabled and the
        entry has citing contexts. A `contradicted` finding is surfaced loudly per citation; the
        (often several) `unverifiable` findings collapse to one line (reliability: report the gap,
        never silently pass); benign `not_in_abstract` / `supported` stay on `audit.alignment_findings`
        for machine consumers and the dedicated report section. Isolated so a failure cannot abort the
        entry.
        """
        if not self.config.check_alignment:
            return
        if verdict is None or verdict.kind != "exactly_one" or not verdict.artifacts:
            return
        contexts = self._citation_contexts.get(audit.entry.key, [])
        if not contexts:
            return
        try:
            findings = await resolve_alignment_findings(
                audit.entry, verdict.artifacts[0], contexts, self.llm, self.config, self.cache
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — advisory; report, don't fail the entry
            audit.issues.append(f"citation alignment check failed (verdict unaffected): {exc}")
            return
        audit.alignment_findings = findings
        for f in findings:
            if f.status == "contradicted":
                audit.issues.append(alignment_note(f))
        unverifiable = sum(1 for f in findings if f.status == "unverifiable")
        if unverifiable:
            first = next(f for f in findings if f.status == "unverifiable")
            audit.issues.append(
                f"citation alignment could not be checked for {unverifiable} citation(s) — {first.detail}"
            )

    def _book_backfill_isbns(self, entry: BibEntry, records: list[SourceRecord]) -> tuple[str, ...]:
        """ISBNs to locate the book in Open Library when the entry carries none of its own.

        A book is often cited by a *chapter-level* DOI (Oxford Scholarship Online mints one DOI per
        chapter: `10.1093/{isbn10}.003.0002`). That DOI's own record carries the containing book's
        ISBNs, and Open Library — which whiffs on subtitle-bearing title searches — resolves the book
        cleanly by ISBN. Return the *whole* ISBN set (OL indexes only some of a book's ISBNs, so we
        try them all). Gated on the entry actually carrying a DOI (this is specifically the chapter-DOI
        case); we trust an ISBN only from a record that provably belongs to this book: the author-cited
        DOI's own record (same published DOI), or an Open Library edition (the authority itself, which
        is what the cached artifact holds when re-deriving from a cached verdict). We deliberately do
        NOT trust a mere same-author record, which could be a different book by the same author."""
        cited_doi = _published_doi(entry.ids.doi)
        if entry.ids.isbn13 or not cited_doi:
            return ()
        for r in records:
            isbns = r.ids.all_isbn13()
            if not isbns:
                continue
            if _published_doi(r.ids.doi) == cited_doi or r.source == "openlibrary":
                return tuple(sorted(isbns))
        return ()

    async def _resolve_book(
        self, audit: EntryAudit, records: list[SourceRecord] | None = None
    ) -> _BookResolution | None:
        """Consult Open Library — the authority of record for books — for the entry's editions.

        Returns None for non-books. Otherwise fetches the editions and selects the cited one
        (`matched`) and the most recent one (`latest`); a transport/HTTP failure is carried as
        `error` (reliability: an outage is reported, never read as 'no editions'). The result drives
        both the identity override (`_apply_book_identity`) and the report (`_report_book`).

        When the entry has no ISBN of its own, an ISBN backfilled from the cited DOI's record (see
        `_book_backfill_isbn`) is used to *fetch* the editions — but the cited edition is still matched
        against the original entry's year/publisher, so a book cited by a chapter DOI grounds on the
        edition the author actually cited (e.g. the 1989 original, not a 1992 reprint the DOI rides on).
        """
        if audit.entry.entry_type not in _NEEDS_ISBN:
            return None
        ol = next((a for a in self.adapters if a.name == "openlibrary"), None)
        if ol is None:
            return _BookResolution(error="Open Library adapter not configured")
        backfilled = self._book_backfill_isbns(audit.entry, records or [])
        query_entry = audit.entry
        if backfilled:
            query_entry = audit.entry.model_copy(
                update={
                    "ids": audit.entry.ids.model_copy(
                        update={"isbn13": backfilled[0], "isbn13s": backfilled}
                    )
                }
            )
        try:
            result = await self._fetch_editions(audit.entry, ol, query_entry=query_entry)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — report, don't fail the entry
            return _BookResolution(error=f"edition lookup failed: {exc}")
        if result.error:
            return _BookResolution(error=result.error)
        editions = result.records
        # Cited edition by the original entry's year/publisher; fall back to the backfilled-ISBN
        # edition so a book whose cited year matches no edition is still confirmed (not a miss).
        matched = match_cited_edition(audit.entry, editions)
        if matched is None and backfilled:
            matched = match_cited_edition(query_entry, editions)
        return _BookResolution(
            editions=editions,
            matched=matched,
            latest=latest_edition(editions),
            backfilled_isbn=(matched.ids.isbn13 if (backfilled and matched) else None),
        )

    def _apply_openalex_identity(self, verdict, entry: BibEntry, records: list[SourceRecord]):
        """Pin the matched artifact to the cited OpenAlex Work when its by-id lookup confirms it.

        A cited openalex.org Work id is an author-supplied identifier, as authoritative as a DOI/ISBN:
        when the lookup returns that exact Work and it passes the title+author gate, that record IS
        the identity. We override here so the explicitly-cited Work — not a similar-titled foreign-DOI
        record the title pooler may have merged it into — represents the entry (which keeps a wrong
        DOI/ISBN from being backfilled). A cited id whose Work has a mismatched title/author does NOT
        confirm (it scores below auto_accept) and is left to the generic verdict.
        """
        if not entry.ids.openalex:
            return verdict
        rec = next(
            (r for r in records if r.ids.openalex and r.ids.openalex == entry.ids.openalex), None
        )
        if rec is None:
            return verdict  # the lookup errored or returned nothing — never read as a confirmation
        features = compute_features(
            entry, rec, tail_threshold=self.config.prefix_trap_tail_jaccard
        )
        if bucket(features, self.config, entry_has_id=True) != "auto_accept":
            return verdict
        artifact = MatchedArtifact(
            records=[rec], versions=[rec], best_record=rec, merged_ids=rec.ids
        )
        return Verdict(
            kind="exactly_one",
            artifacts=[artifact],
            confidence="high",
            rationale="confirmed via OpenAlex (cited Work id resolved)",
        )

    def _apply_google_books_identity(self, verdict, entry: BibEntry, records: list[SourceRecord]):
        """Pin the matched artifact to the cited Google Books volume when its by-id lookup confirms it.

        Mirrors `_apply_openalex_identity`: a cited volume id is an author-supplied key, so when the
        lookup returns that exact volume and it passes the title+author gate, that record IS the
        identity — overriding a same-titled journal-article a title pooler may otherwise match (a book
        review carries the book's title+authors AND a DOI, which would be wrongly backfilled onto a
        @book). A volume whose title/author mismatches scores below auto_accept and is left alone.
        """
        if not entry.ids.google_books:
            return verdict
        rec = next(
            (
                r
                for r in records
                if r.ids.google_books and r.ids.google_books == entry.ids.google_books
            ),
            None,
        )
        if rec is None:
            return verdict  # the lookup errored or returned nothing — never read as a confirmation
        features = compute_features(
            entry, rec, tail_threshold=self.config.prefix_trap_tail_jaccard
        )
        if bucket(features, self.config, entry_has_id=True) != "auto_accept":
            return verdict
        artifact = MatchedArtifact(
            records=[rec], versions=[rec], best_record=rec, merged_ids=rec.ids
        )
        return Verdict(
            kind="exactly_one",
            artifacts=[artifact],
            confidence="high",
            rationale="confirmed via Google Books (cited volume id resolved)",
        )

    def _apply_book_identity(self, verdict, book: _BookResolution | None):
        """For a book, an Open Library edition match is authoritative identity: it confirms the cited
        edition as the matched artifact, overriding the article-centric matcher (which can't bridge a
        1976 original to its only DOI-bearing record, a 2018 reprint). Non-books and unconfirmed books
        keep whatever the generic matcher decided.

        One exception, for reliability: when Open Library — the book authority of record — could not be
        reached (`book.error`), a `none` ('possible hallucination') verdict from the article-centric
        matcher is NOT trustworthy. That matcher routinely fails to confirm a real book (it sees only a
        reprint, or nothing), so without the authority we have not actually checked the book. Leave it
        unresolved (reported, retried next run) rather than assert a hallucination we did not establish.
        """
        if book is None:
            return verdict
        if book.matched is None:
            if book.error and verdict is not None and verdict.kind == "none":
                return None
            return verdict
        edition = book.matched
        artifact = MatchedArtifact(
            records=[edition], versions=[edition], best_record=edition, merged_ids=edition.ids
        )
        return Verdict(
            kind="exactly_one",
            artifacts=[artifact],
            confidence="high",
            rationale="confirmed via Open Library (cited edition matched)",
        )

    async def _report_book(self, audit: EntryAudit, book: _BookResolution | None) -> None:
        """Report the Open Library book check: confirm the cited edition (or raise the gap to the
        user), check its year/publisher, and point at the latest edition as a better version.

        Runs after `_check_fields` so it appends to — never clobbers — the field findings.
        """
        if book is None:
            return
        cited = describe_cited_edition(audit.entry)
        if book.error:
            audit.issues.append(
                f"could not verify the cited edition ({cited}) against Open Library "
                f"(reported, will retry next run): {book.error}"
            )
            return
        if not book.editions:
            audit.issues.append(
                f"could not verify the cited edition ({cited}) against Open Library "
                "— the book was not found there"
            )
            return
        if book.matched is None:
            audit.issues.append(
                f"the cited edition ({cited}) was not found among Open Library's "
                f"{len(book.editions)} known edition(s) — verify the year/publisher"
            )
            return

        # Step 1: year/publisher correctness, grounded in the confirmed cited edition.
        if self.config.check_fields:
            try:
                findings = await check_book_edition_fields(
                    audit.entry, book.matched, self.llm, self.config, self.cache
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — advisory; report, don't fail the entry
                audit.issues.append(f"book edition field check failed (verdict unaffected): {exc}")
                findings = []
            audit.field_findings = list(audit.field_findings) + findings
            for f in findings:
                if f.status in ("error", "uncertain"):
                    audit.issues.append(finding_note(f))

        if book.backfilled_isbn is not None:
            audit.issues.append(
                f"the cited DOI ({audit.entry.ids.doi}) is a chapter/component DOI; the book itself "
                f"was confirmed via Open Library by its ISBN ({book.backfilled_isbn}) — consider "
                "citing the book's ISBN or book-level DOI"
            )

        # Step 2: a later edition than the one cited is a better version to consider.
        note = better_edition_note(book.matched, book.latest)
        if note:
            audit.issues.append(note)

    async def _fetch_editions(
        self, entry: BibEntry, ol: SourceAdapter, *, query_entry: BibEntry | None = None
    ):
        """Open Library editions for `entry` (cached per entry, like any source query).

        `query_entry` (default `entry`) is what's actually sent to Open Library — it may carry an ISBN
        backfilled from the cited DOI — while the cache key stays the *original* entry's hash, so the
        cached-verdict re-derivation reuses the same editions regardless of how the ISBN was found."""
        if self.cache is not None:
            cached = self.cache.get_source_query(entry.content_hash, ol.name, "editions")
            if cached is not None:
                return cached
        result = await ol.fetch_editions(query_entry or entry)
        if self.cache is not None:
            self.cache.put_source_query(entry.content_hash, result)
        return result


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
