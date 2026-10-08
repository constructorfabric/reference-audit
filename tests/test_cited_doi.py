"""The cited-DOI check: on an `exactly_one` match, is the DOI the entry cites the matched work's?

Identification can match a real work on title and authors while the cited DOI points at another paper
or at nothing. These tests drive `AuditPipeline._check_cited_doi` with stub sources (no network), and
one end-to-end run that also checks the HALLMARK `strict` mapping reads the finding as HALLUCINATED.
"""

from __future__ import annotations

import hallmark_bench as hb
from pydantic import BaseModel

from reference_audit.config import AuditConfig
from reference_audit.models import (
    BibEntry,
    CanCorrespondResult,
    EntryType,
    FieldJudgment,
    Identifiers,
    MatchedArtifact,
    SameWorkResult,
    SourceQueryResult,
    SourceRecord,
)
from reference_audit.pipeline import AuditPipeline
from reference_audit.sources.base import SourceAdapter

TITLE = "Toward a theory of evolution as multilevel learning"
AUTHORS = ["Vitaly Vanchurin", "Yuri I. Wolf", "Mikhail I. Katsnelson", "Eugene V. Koonin"]
REAL_DOI = "10.1073/pnas.2120037119"
OTHER_DOI = "10.1016/j.cell.2020.01.001"


def _rec(doi: str | None, title: str = TITLE, authors: list[str] = AUTHORS, source="crossref"):
    return SourceRecord(
        source=source, source_native_id=doi or title, title=title, authors=authors, year=2022,
        venue="Proceedings of the National Academy of Sciences", ids=Identifiers(doi=doi),
    )


class StubCrossref(SourceAdapter):
    """By-id answers from `by_doi`; metadata search returns `search`; `error` fails every by-id call."""

    name = "crossref"
    handles = set(EntryType)

    def __init__(self, by_doi=None, search=None, error=None):
        super().__init__(client=_NoClient())
        self.by_doi = by_doi or {}
        self.search = search or []
        self.error = error
        self.id_calls: list[str] = []

    async def lookup_by_id(self, ids: Identifiers) -> SourceQueryResult:
        self.id_calls.append(ids.doi or "")
        if self.error:
            return SourceQueryResult(source=self.name, query_kind="id", error=self.error)
        rec = self.by_doi.get(ids.doi or "")
        return SourceQueryResult(source=self.name, query_kind="id", records=[rec] if rec else [])

    async def search_by_metadata(self, entry, limit=10) -> SourceQueryResult:
        return SourceQueryResult(source=self.name, query_kind="metadata", records=list(self.search))


class StubPublisher(SourceAdapter):
    """doi.org's answer for every DOI: True (registered), False (404) or None (unreachable)."""

    name = "publisher"
    handles: set = set()

    def __init__(self, registered: bool | None):
        super().__init__(client=_NoClient())
        self.registered = registered

    async def doi_registered(self, doi: str) -> bool | None:
        return self.registered


class _NoClient:
    async def aclose(self):
        pass


def _pipeline(*adapters) -> AuditPipeline:
    return AuditPipeline(AuditConfig(model="test", use_llm=False), adapters=list(adapters))


def _entry(doi: str, entry_type=EntryType.ARTICLE) -> BibEntry:
    return BibEntry(
        key="k", entry_type=entry_type, title=TITLE, authors=AUTHORS, year=2022,
        ids=Identifiers(doi=doi),
    )


def _artifact(*records: SourceRecord) -> MatchedArtifact:
    return MatchedArtifact(
        records=list(records), versions=list(records), best_record=records[0],
        merged_ids=records[0].ids,
    )


async def test_the_matched_work_carrying_the_cited_doi_is_ok():
    pipe = _pipeline(StubCrossref())
    f = await pipe._check_cited_doi(_entry(REAL_DOI), _artifact(_rec(REAL_DOI)))
    assert (f.field, f.status, f.bib_value) == ("doi", "ok", REAL_DOI)
    assert pipe.adapters[0].id_calls == []  # nothing to look up


async def test_a_cited_doi_folded_into_a_pooled_record_is_ok():
    # Pooling keeps the arXiv DOI of the (more cited) preprint as `ids.doi`; the published DOI the
    # entry cites survives in `merged_dois`.
    from reference_audit.matching.pool import pool_candidates

    preprint = _rec("10.48550/arxiv.2301.01404", source="semantic_scholar").model_copy(
        update={"is_preprint": True, "citation_count": 50}
    )
    (pooled,) = pool_candidates([preprint, _rec(REAL_DOI)])
    assert pooled.ids.doi == "10.48550/arxiv.2301.01404"
    pipe = _pipeline(StubCrossref())
    f = await pipe._check_cited_doi(_entry(REAL_DOI), _artifact(pooled))
    assert f.status == "ok"


async def test_a_cited_doi_of_another_work_is_an_error_naming_it():
    frogs = _rec(OTHER_DOI, title="Frog limb regeneration", authors=["A. Frog"])
    pipe = _pipeline(StubCrossref(by_doi={OTHER_DOI: frogs}), StubPublisher(True))
    f = await pipe._check_cited_doi(_entry(OTHER_DOI), _artifact(_rec(REAL_DOI)))
    assert f.status == "error"
    assert "different work: 'Frog limb regeneration'" in f.detail
    assert REAL_DOI in f.detail and f.canonical_value == REAL_DOI
    assert f.sources == ["crossref"]


async def test_a_cited_doi_of_an_unmerged_same_titled_record_is_uncertain():
    twin = _rec(OTHER_DOI, source="crossref")
    pipe = _pipeline(StubCrossref(by_doi={OTHER_DOI: twin}))
    f = await pipe._check_cited_doi(_entry(OTHER_DOI), _artifact(_rec(REAL_DOI, source="openalex")))
    assert f.status == "uncertain" and "same title and authors" in f.detail


async def test_a_cited_doi_that_404s_at_doi_org_is_an_error():
    pipe = _pipeline(StubCrossref(), StubPublisher(False))
    f = await pipe._check_cited_doi(_entry(OTHER_DOI), _artifact(_rec(REAL_DOI)))
    assert f.status == "error" and "does not resolve at doi.org" in f.detail


async def test_a_registered_doi_no_source_knows_is_uncertain():
    pipe = _pipeline(StubCrossref(), StubPublisher(True))
    f = await pipe._check_cited_doi(_entry(OTHER_DOI), _artifact(_rec(REAL_DOI)))
    assert f.status == "uncertain" and "could not be tied to the matched work" in f.detail


async def test_an_unanswerable_doi_check_is_unverifiable_with_the_cause():
    pipe = _pipeline(StubCrossref(error="crossref: HTTP 503"), StubPublisher(None))
    f = await pipe._check_cited_doi(_entry(OTHER_DOI), _artifact(_rec(REAL_DOI)))
    assert f.status == "unverifiable"
    assert "crossref: HTTP 503" in f.detail and "doi.org could not be reached" in f.detail


async def test_books_and_arxiv_dois_are_not_checked():
    pipe = _pipeline(StubCrossref())
    art = _artifact(_rec(REAL_DOI))
    assert await pipe._check_cited_doi(_entry(OTHER_DOI, EntryType.BOOK), art) is None
    assert await pipe._check_cited_doi(_entry("10.48550/arxiv.2303.17548"), art) is None


class _AcceptingLLM:
    """Confirms every candidate but the frog paper; calls every field difference a formatting variant."""

    async def structured(self, system: str, user: str, schema: type[BaseModel], kind: str):
        if schema is CanCorrespondResult:
            same = "Frog limb" not in user
            return CanCorrespondResult(can_correspond=same, confidence="high", reason="by title")
        if schema is SameWorkResult:
            return SameWorkResult(relation="same_artifact", confidence="high", reason="same")
        if schema is FieldJudgment:
            return FieldJudgment(classification="formatting_variant", confidence="high", reason="ok")
        raise AssertionError(schema)

    async def aclose(self):
        pass


async def test_end_to_end_a_wrong_doi_is_reported_and_strict_scores_it_hallucinated(tmp_path):
    frogs = _rec(OTHER_DOI, title="Frog limb regeneration", authors=["A. Frog"])
    crossref = StubCrossref(by_doi={OTHER_DOI: frogs}, search=[_rec(REAL_DOI)])
    pipe = AuditPipeline(
        AuditConfig(model="test"), adapters=[crossref, StubPublisher(True)], llm=_AcceptingLLM(),
    )
    bib = tmp_path / "r.bib"
    bib.write_text(
        "@article{k, title={" + TITLE + "}, author={" + " and ".join(AUTHORS) + "},"
        " journal={Proceedings of the National Academy of Sciences}, year={2022},"
        " doi={" + OTHER_DOI + "}}\n",
        encoding="utf-8",
    )
    report = await pipe.run(None, bib)
    await pipe.aclose()
    (audit,) = report.entries
    assert audit.verdict.kind == "exactly_one"
    (doi,) = [f for f in audit.field_findings if f.field == "doi"]
    assert doi.status == "error"
    assert any("field 'doi' looks wrong" in i for i in audit.issues)
    compact = hb.compact(audit)
    assert hb.predict(compact, "identity").label == "VALID"
    assert hb.predict(compact, "strict").label == "HALLUCINATED"


async def test_enrichment_by_a_backfilled_doi_does_not_overwrite_the_entrys_id_lookup(tmp_path):
    # An entry with no identifier matched by title: enrichment looks the backfilled DOI up. Cached
    # under the entry, that lookup would be read back as the entry's own by-id result next run.
    from reference_audit.cache.store import AuditCache

    cache = AuditCache(tmp_path / "c.db", model="test")
    crossref = StubCrossref(by_doi={REAL_DOI: _rec(REAL_DOI)}, search=[_rec(REAL_DOI)])
    pipe = AuditPipeline(AuditConfig(model="test", use_llm=False), cache=cache, adapters=[crossref])
    bib = tmp_path / "r.bib"
    bib.write_text(
        "@article{k, title={" + TITLE + "}, author={" + " and ".join(AUTHORS) + "}, year={2022}}\n",
        encoding="utf-8",
    )
    (audit,) = (await pipe.run(None, bib)).entries
    await pipe.aclose()
    assert audit.verdict.kind == "exactly_one" and crossref.id_calls == [REAL_DOI]
    assert cache.get_source_query(audit.entry.content_hash, "crossref", "id") is None
    cache.close()
