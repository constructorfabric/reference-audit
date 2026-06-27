"""M4 LLM adjudication with a fake (in-memory) LLM client — no network."""

import httpx
import respx

from reference_audit.cache.store import AuditCache
from reference_audit.config import AuditConfig
from reference_audit.llm.schemas import strict_schema
from reference_audit.matching.adjudicate import adjudicate_entry
from reference_audit.models import (
    BibEntry,
    CanCorrespondResult,
    CandidateAssessment,
    EntryAudit,
    EntryType,
    FeatureVector,
    Identifiers,
    SourceRecord,
)
from reference_audit.pipeline import AuditPipeline
from reference_audit.sources.crossref import CrossrefAdapter


class FakeLLM:
    """Programmable stand-in for LLMClient. `decider(user)` → CanCorrespondResult | 'raise'."""

    def __init__(self, decider):
        self.decider = decider
        self.calls = 0

    async def structured(self, system, user, schema_model, schema_name):
        self.calls += 1
        out = self.decider(user)
        if out == "raise":
            from reference_audit.llm.client import LLMError

            raise LLMError("boom")
        return out

    async def aclose(self):
        pass


def _adj_candidate(title="Quantum Foo Methods for Bar", doi="10.5/bar"):
    rec = SourceRecord(source="crossref", title=title, authors=["A. Author"],
                       ids=Identifiers(doi=doi))
    return CandidateAssessment(
        record=rec,
        features=FeatureVector(title_ratio=0.85, author_overlap=1.0, composite=0.7,
                               id_agreement="absent"),
        bucket="adjudicate",
    )


def _audit(*candidates):
    entry = BibEntry(key="e", entry_type=EntryType.ARTICLE, title="Quantum Foo Methods",
                     authors=["Author, A."])
    return EntryAudit(entry=entry, candidates=list(candidates))


# ── schema ────────────────────────────────────────────────────────────────────
def test_strict_schema_all_required_no_additional():
    s = strict_schema(CanCorrespondResult)
    assert s["additionalProperties"] is False
    # every property — including the defaulted distinguishing_evidence — is required in strict mode
    assert set(s["required"]) == set(s["properties"].keys())
    assert "distinguishing_evidence" in s["required"]


# ── adjudicator ────────────────────────────────────────────────────────────────
async def test_adjudicate_promotes_affirmed():
    audit = _audit(_adj_candidate())
    llm = FakeLLM(lambda u: CanCorrespondResult(can_correspond=True, confidence="high", reason="same"))
    errored = await adjudicate_entry(audit, llm, AuditConfig(model="t"), None)
    assert errored is False
    assert audit.candidates[0].bucket == "auto_accept"
    assert audit.candidates[0].llm.can_correspond is True


async def test_adjudicate_confident_reject_demotes_to_auto_reject():
    audit = _audit(_adj_candidate())
    llm = FakeLLM(lambda u: CanCorrespondResult(can_correspond=False, confidence="high", reason="diff"))
    await adjudicate_entry(audit, llm, AuditConfig(model="t"), None)
    # confident non-match is decisive → auto_reject (lets the entry conclude `none`)
    assert audit.candidates[0].bucket == "auto_reject"


async def test_adjudicate_low_confidence_not_promoted():
    audit = _audit(_adj_candidate())
    llm = FakeLLM(lambda u: CanCorrespondResult(can_correspond=True, confidence="low", reason="maybe"))
    await adjudicate_entry(audit, llm, AuditConfig(model="t"), None)
    assert audit.candidates[0].bucket == "adjudicate"


async def test_adjudicate_error_flagged():
    audit = _audit(_adj_candidate())
    llm = FakeLLM(lambda u: "raise")
    errored = await adjudicate_entry(audit, llm, AuditConfig(model="t"), None)
    assert errored is True
    assert audit.candidates[0].bucket == "adjudicate"


async def test_adjudicate_decision_cached(tmp_path):
    cache = AuditCache(tmp_path / "c.db", model="t")
    audit = _audit(_adj_candidate())
    llm = FakeLLM(lambda u: CanCorrespondResult(can_correspond=True, confidence="high", reason="s"))
    await adjudicate_entry(audit, llm, AuditConfig(model="t"), cache)
    assert llm.calls == 1
    # second adjudication of the same candidate → served from cache, no new call
    audit2 = _audit(_adj_candidate())
    await adjudicate_entry(audit2, llm, AuditConfig(model="t"), cache)
    assert llm.calls == 1
    assert audit2.candidates[0].bucket == "auto_accept"
    cache.close()


# ── pipeline integration (mocked Crossref + fake LLM) ───────────────────────────
DOILESS = (
    "@article{q, title={Quantum Foo Methods}, author={Author, A.}, journal={J}, year={2020}}"
)
CR_ITEM = {
    "DOI": "10.5/bar",
    "title": ["Quantum Foo Methods for Bar Systems"],  # ~0.85 title → adjudicate, not Path B
    "author": [{"given": "A.", "family": "Author"}],
    "issued": {"date-parts": [[2020]]},
    "type": "journal-article",
}


@respx.mock
async def test_pipeline_llm_resolves_unresolved(tmp_path):
    respx.get(url__regex=r"api\.crossref\.org/works\?").mock(
        return_value=httpx.Response(200, json={"message": {"items": [CR_ITEM]}})
    )
    bib = tmp_path / "r.bib"
    bib.write_text(DOILESS, encoding="utf-8")
    cache = AuditCache(tmp_path / "c.db", model="t")
    llm = FakeLLM(lambda u: CanCorrespondResult(can_correspond=True, confidence="high", reason="same"))
    pipe = AuditPipeline(
        AuditConfig(model="t"), cache=cache,
        adapters=[CrossrefAdapter(client=httpx.AsyncClient())], llm=llm,
    )
    report = await pipe.run(None, bib)
    await pipe.aclose()
    cache.close()
    assert llm.calls >= 1
    assert report.entries[0].verdict.kind == "exactly_one"


@respx.mock
async def test_pipeline_llm_rejects_all_is_none(tmp_path):
    # T4: the only candidate is a different paper; LLM rejects → none (not forced match)
    respx.get(url__regex=r"api\.crossref\.org/works\?").mock(
        return_value=httpx.Response(200, json={"message": {"items": [CR_ITEM]}})
    )
    bib = tmp_path / "r.bib"
    bib.write_text(DOILESS, encoding="utf-8")
    cache = AuditCache(tmp_path / "c.db", model="t")
    llm = FakeLLM(lambda u: CanCorrespondResult(can_correspond=False, confidence="high", reason="diff"))
    pipe = AuditPipeline(
        AuditConfig(model="t"), cache=cache,
        adapters=[CrossrefAdapter(client=httpx.AsyncClient())], llm=llm,
    )
    report = await pipe.run(None, bib)
    await pipe.aclose()
    cache.close()
    assert report.entries[0].verdict.kind == "none"
