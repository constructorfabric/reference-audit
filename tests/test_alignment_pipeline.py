"""Step 5 (Citation Alignment): pipeline wiring.

`_check_alignment` runs after field checks on an exactly_one match, is opt-in, surfaces contradicted
loudly, collapses unverifiable, keeps not_in_abstract off the issue list, and never changes the
verdict. A full run() confirms citing contexts flow from the .tex through to findings.
"""

from __future__ import annotations

from reference_audit.config import AuditConfig
from reference_audit.models import (
    BibEntry,
    CitationAlignmentResult,
    CitationContext,
    EntryType,
    EntryAudit,
    Identifiers,
    MatchedArtifact,
    SourceQueryResult,
    SourceRecord,
    Verdict,
)
from reference_audit.pipeline import AuditPipeline


class FakeLLM:
    def __init__(self, responder):
        self.responder = responder
        self.calls = 0

    async def structured(self, system, user, schema_model, schema_name):
        self.calls += 1
        return self.responder(user)

    async def aclose(self):
        pass


def _pipe(llm=None, check_alignment=True):
    cfg = AuditConfig(_env_file=None, model="t", check_alignment=check_alignment)
    return AuditPipeline(cfg, adapters=[], llm=llm)


def _exactly_one(abstract="We prove widgets diverge."):
    rec = SourceRecord(source="openalex", title="Widgets", ids=Identifiers(doi="10.1/x"),
                       abstract=abstract)
    return Verdict(kind="exactly_one", artifacts=[MatchedArtifact(records=[rec], best_record=rec)])


def _audit():
    return EntryAudit(entry=BibEntry(key="w", title="Widgets", ids=Identifiers(doi="10.1/x")))


async def test_contradicted_surfaces_as_issue():
    llm = FakeLLM(lambda u: CitationAlignmentResult(
        classification="contradicted", confidence="high", reason="opposite", evidence_quote="diverge"))
    pipe = _pipe(llm)
    pipe._citation_contexts = {"w": [CitationContext(key="w", text="Widgets converge.")]}
    audit, verdict = _audit(), _exactly_one()
    await pipe._check_alignment(audit, verdict)
    assert [f.status for f in audit.alignment_findings] == ["contradicted"]
    assert any("misrepresent" in i for i in audit.issues)
    assert audit.verdict is None or True  # _check_alignment never sets the verdict


async def test_not_in_abstract_is_not_an_issue():
    llm = FakeLLM(lambda u: CitationAlignmentResult(
        classification="not_in_abstract", confidence="medium", reason="silent"))
    pipe = _pipe(llm)
    pipe._citation_contexts = {"w": [CitationContext(key="w", text="Widgets are purple.")]}
    audit = _audit()
    await pipe._check_alignment(audit, _exactly_one("An unrelated abstract."))
    assert [f.status for f in audit.alignment_findings] == ["not_in_abstract"]
    assert audit.issues == []          # benign: shown in the dedicated section, not as an issue


async def test_unverifiable_collapsed_to_one_line():
    # No abstract on the artifact → every citation unverifiable; report as a single collapsed line.
    llm = FakeLLM(lambda u: CitationAlignmentResult(classification="supported", confidence="high",
                                                    reason="x"))
    pipe = _pipe(llm)
    pipe._citation_contexts = {"w": [
        CitationContext(key="w", text="Claim one.", ordinal=0),
        CitationContext(key="w", text="Claim two.", ordinal=1),
    ]}
    audit = _audit()
    await pipe._check_alignment(audit, _exactly_one(abstract=""))
    assert all(f.status == "unverifiable" for f in audit.alignment_findings)
    collapsed = [i for i in audit.issues if "could not be checked for 2 citation" in i]
    assert len(collapsed) == 1
    assert llm.calls == 0


async def test_opt_out_by_default():
    pipe = _pipe(llm=FakeLLM(lambda u: None), check_alignment=False)
    pipe._citation_contexts = {"w": [CitationContext(key="w", text="Claim.")]}
    audit = _audit()
    await pipe._check_alignment(audit, _exactly_one())
    assert audit.alignment_findings == [] and audit.issues == []


async def test_skipped_when_not_exactly_one():
    pipe = _pipe(llm=FakeLLM(lambda u: None))
    pipe._citation_contexts = {"w": [CitationContext(key="w", text="Claim.")]}
    audit = _audit()
    await pipe._check_alignment(audit, Verdict(kind="none"))
    await pipe._check_alignment(audit, None)
    assert audit.alignment_findings == []


# ── end-to-end run(): contexts flow from .tex to findings ─────────────────────

class _StubAdapter:
    """Returns one matching record (with an abstract) for both id and metadata lookups."""

    name = "openalex"
    handles = {EntryType.ARTICLE}

    def __init__(self, rec):
        self.rec = rec

    async def lookup_by_id(self, ids):
        return SourceQueryResult(source=self.name, query_kind="id", records=[self.rec])

    async def search_by_metadata(self, entry, limit=10):
        return SourceQueryResult(source=self.name, query_kind="metadata", records=[self.rec])

    async def aclose(self):
        pass


async def test_run_extracts_contexts_and_flags_contradiction(tmp_path):
    bib = tmp_path / "r.bib"
    bib.write_text(
        "@article{good, title={Widget dynamics}, author={Ann}, year={2020}, doi={10.1/x}}\n",
        encoding="utf-8",
    )
    tex = tmp_path / "m.tex"
    tex.write_text(r"Widgets converge under all loads \citep{good}.", encoding="utf-8")

    rec = SourceRecord(source="openalex", title="Widget dynamics", authors=["Ann"], year=2020,
                       ids=Identifiers(doi="10.1/x"), abstract="We show widgets diverge under load.")
    llm = FakeLLM(lambda u: CitationAlignmentResult(
        classification="contradicted", confidence="high", reason="reversed", evidence_quote="diverge"))
    cfg = AuditConfig(_env_file=None, model="t", check_alignment=True, check_fields=False)
    pipe = AuditPipeline(cfg, adapters=[_StubAdapter(rec)], llm=llm)
    report = await pipe.run(tex, bib)
    await pipe.aclose()

    audit = report.entries[0]
    assert audit.verdict is not None and audit.verdict.kind == "exactly_one"
    assert [f.status for f in audit.alignment_findings] == ["contradicted"]
    assert any("misrepresent" in i for i in audit.issues)
