"""An unresolved entry says why, and a `none` verdict on a local snapshot names its coverage end.

Stub sources only (no network)."""

from __future__ import annotations

from datetime import date

from reference_audit.config import AuditConfig
from reference_audit.models import EntryType, Identifiers, SourceQueryResult, SourceRecord
from reference_audit.pipeline import AuditPipeline
from reference_audit.sources.base import SourceAdapter


class _NoClient:
    async def aclose(self):
        pass


class StubSearch(SourceAdapter):
    """A metadata-only source returning fixed records; `coverage_end` marks it a local snapshot."""

    name = "openalex"
    handles = set(EntryType)

    def __init__(self, records=(), coverage_end: date | None = None):
        super().__init__(client=_NoClient())
        self.records = list(records)
        self.coverage_end = coverage_end

    async def search_by_metadata(self, entry, limit=10) -> SourceQueryResult:
        return SourceQueryResult(source=self.name, query_kind="metadata", records=self.records)


def _bib(tmp_path, year=2021):
    p = tmp_path / "r.bib"
    p.write_text(
        "@article{k, title={Learning to rank citations with graph transformers}, "
        f"author={{Doe, Jane and Roe, Kim}}, journal={{Journal of Ranking}}, year={{{year}}}}}\n",
        encoding="utf-8",
    )
    return p


async def test_a_candidate_left_for_a_disabled_llm_is_named(tmp_path):
    # Same title, different authors and year: neither accepted nor rejected by the formal rules.
    near = SourceRecord(
        source="openalex", source_native_id="W1",
        title="Learning to rank citations with graph transformers", authors=["Ann Other"],
        year=2019, ids=Identifiers(doi="10.1/x"),
    )
    pipe = AuditPipeline(AuditConfig(model="t", use_llm=False), adapters=[StubSearch([near])])
    report = await pipe.run(None, _bib(tmp_path))
    await pipe.aclose()
    (audit,) = report.entries
    assert audit.verdict is None
    assert audit.unresolved_reasons == [
        "1 candidate(s) need LLM adjudication, but the LLM is disabled (--no-llm)"
    ]
    assert "unresolved: 1 candidate(s) need LLM adjudication" in " ".join(audit.issues)


async def test_a_resolved_entry_has_no_unresolved_reasons(tmp_path):
    pipe = AuditPipeline(AuditConfig(model="t", use_llm=False), adapters=[StubSearch()])
    report = await pipe.run(None, _bib(tmp_path))
    await pipe.aclose()
    (audit,) = report.entries
    assert audit.verdict.kind == "none" and audit.unresolved_reasons == []


async def test_a_no_match_from_a_snapshot_that_may_predate_the_work_says_so(tmp_path):
    snapshot = StubSearch(coverage_end=date(2024, 6, 26))
    pipe = AuditPipeline(AuditConfig(model="t", use_llm=False), adapters=[snapshot])
    recent = (await pipe.run(None, _bib(tmp_path, year=2024))).entries[0]
    old = (await pipe.run(None, _bib(tmp_path, year=2021))).entries[0]
    report = await pipe.run(None, _bib(tmp_path, year=2021))
    await pipe.aclose()
    assert recent.verdict.kind == "none"
    assert any("openalex up to 2024-06-26" in i and "coverage gap" in i for i in recent.issues)
    assert not any("coverage gap" in i for i in old.issues)
    assert report.summary["source_coverage"] == {"openalex": "2024-06-26"}
