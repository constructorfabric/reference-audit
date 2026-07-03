"""Step 4 (Citation Alignment): the classification funnel, with a mocked LLM.

Covers the taxonomy (supported / contradicted / not_in_abstract), the reliability rules (no abstract,
no LLM, LLM error, unexpected error → unverifiable, never contradicted; per-citation isolation), and
decision caching.
"""

from __future__ import annotations

import pytest

from reference_audit.alignmentcheck import (
    artifact_abstract,
    resolve_alignment_findings,
)
from reference_audit.cache.store import AuditCache
from reference_audit.config import AuditConfig
from reference_audit.llm.client import LLMError
from reference_audit.models import (
    BibEntry,
    CitationAlignmentResult,
    CitationContext,
    MatchedArtifact,
    SourceRecord,
)

_CONFIG = AuditConfig(_env_file=None, model="t")


def _entry():
    return BibEntry(key="x", title="Widget dynamics", authors=["Ann"], year=2020)


def _artifact(abstract="We prove widgets converge."):
    return MatchedArtifact(records=[SourceRecord(source="openalex", abstract=abstract)])


def _ctx(text, ordinal=0):
    return CitationContext(key="x", text=text, ordinal=ordinal)


class FakeLLM:
    """responder(user) -> CitationAlignmentResult, or raises to simulate a failure."""

    def __init__(self, responder):
        self.responder = responder
        self.calls = 0

    async def structured(self, system, user, schema_model, schema_name):
        self.calls += 1
        return self.responder(user)

    async def aclose(self):
        pass


def _const(result):
    return FakeLLM(lambda user: result)


async def _resolve(entry, artifact, contexts, llm, cache=None):
    return await resolve_alignment_findings(entry, artifact, contexts, llm, _CONFIG, cache)


async def test_supported():
    llm = _const(CitationAlignmentResult(classification="supported", confidence="high",
                                         reason="matches", evidence_quote="widgets converge"))
    (f,) = await _resolve(_entry(), _artifact(), [_ctx("Widgets converge under load.")], llm)
    assert f.status == "supported" and f.via_llm and f.evidence_quote == "widgets converge"


async def test_contradicted_carries_evidence():
    llm = _const(CitationAlignmentResult(classification="contradicted", confidence="high",
                                         reason="opposite", evidence_quote="widgets diverge"))
    (f,) = await _resolve(_entry(), _artifact("Actually widgets diverge."),
                          [_ctx("Widgets converge under load.")], llm)
    assert f.status == "contradicted" and f.evidence_quote == "widgets diverge"


async def test_silent_abstract_is_not_in_abstract_never_contradicted():
    # The model, correctly prompted, returns not_in_abstract when the abstract is silent.
    llm = _const(CitationAlignmentResult(classification="not_in_abstract", confidence="medium",
                                         reason="abstract silent"))
    (f,) = await _resolve(_entry(), _artifact("An unrelated summary."),
                          [_ctx("Widgets are purple.")], llm)
    assert f.status == "not_in_abstract"
    assert f.status != "contradicted"


async def test_no_abstract_is_unverifiable_without_calling_llm():
    llm = _const(CitationAlignmentResult(classification="supported", confidence="high", reason="x"))
    (f,) = await _resolve(_entry(), _artifact(abstract=""), [_ctx("A claim.")], llm)
    assert f.status == "unverifiable"
    assert "no abstract" in f.detail
    assert llm.calls == 0            # never guesses when there is nothing to check against


async def test_llm_unavailable_is_unverifiable():
    (f,) = await _resolve(_entry(), _artifact(), [_ctx("A claim.")], None)
    assert f.status == "unverifiable" and "LLM unavailable" in f.detail


async def test_llm_error_is_unverifiable_never_contradicted():
    def boom(user):
        raise LLMError("boom")
    (f,) = await _resolve(_entry(), _artifact(), [_ctx("A claim.")], FakeLLM(boom))
    assert f.status == "unverifiable"
    assert f.status != "contradicted"


async def test_per_citation_isolation():
    # One context's classifier blows up; the others must still be classified.
    def responder(user):
        if "explode" in user:
            raise RuntimeError("kaboom")
        return CitationAlignmentResult(classification="supported", confidence="high", reason="ok")
    contexts = [_ctx("please explode here", 0), _ctx("a fine claim", 1)]
    findings = await _resolve(_entry(), _artifact(), contexts, FakeLLM(responder))
    by_ord = {f.ordinal: f for f in findings}
    assert by_ord[0].status == "unverifiable"      # the failing one, isolated
    assert by_ord[1].status == "supported"         # the other still classified


async def test_decision_is_cached(tmp_path):
    cache = AuditCache(tmp_path / "c.db", pipeline_version="t", model="t")
    llm = _const(CitationAlignmentResult(classification="supported", confidence="high", reason="ok"))
    ctx = [_ctx("A stable claim.")]
    first = await _resolve(_entry(), _artifact(), ctx, llm, cache)
    second = await _resolve(_entry(), _artifact(), ctx, llm, cache)
    assert first[0].status == second[0].status == "supported"
    assert llm.calls == 1            # second run served from the decision cache
    cache.close()


async def test_empty_contexts_returns_no_findings():
    assert await _resolve(_entry(), _artifact(), [], _const(None)) == []


def test_artifact_abstract_scans_all_records():
    art = MatchedArtifact(
        records=[SourceRecord(source="crossref", abstract="")],
        versions=[SourceRecord(source="openalex", abstract="the real abstract")],
    )
    assert artifact_abstract(art) == "the real abstract"
    assert artifact_abstract(MatchedArtifact()) == ""
