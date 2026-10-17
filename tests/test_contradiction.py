"""Hard-ID contradictions retain evidence and never become a guessed metadata match."""

import json

import pytest

import hallmark_bench as hb
from reference_audit.cache.store import AuditCache
from reference_audit.config import AuditConfig
from reference_audit.models import (
    AuditReport, BibEntry, EntryAudit, EntryType, FieldJudgment, Identifiers, SourceQueryResult, SourceRecord,
    CanCorrespondResult,
)
from reference_audit.pipeline import AuditPipeline
from reference_audit.report import render_json, render_text
from reference_audit.sources.base import SourceAdapter


class Source(SourceAdapter):
    name = "crossref"
    handles = set(EntryType)

    def __init__(self, records=(), search=(), error=None):
        self.records, self.search, self.error = list(records), list(search), error

    async def lookup_by_id(self, ids):
        return SourceQueryResult(source=self.name, query_kind="id", records=self.records,
                                 error=self.error)

    async def search_by_metadata(self, entry, limit=10):
        return SourceQueryResult(source=self.name, query_kind="metadata", records=self.search)

    async def aclose(self):
        pass


def record(**changes):
    return SourceRecord(source="crossref", title="Masked Autoencoders Are Scalable Vision Learners",
                        authors=["Kaiming He", "Xinlei Chen"], year=2022,
                        ids=Identifiers(doi="10.1109/cvpr52688.2022.01553")).model_copy(update=changes)


async def run(tmp_path, source, *, cache=None, check_fields=True, title=None, authors=None,
              year=2022, llm=None, ids=None, raw_fields=None, pages="", partial_authors="warn"):
    entry = BibEntry(key="k", entry_type=EntryType.INPROCEEDINGS,
                     title=title if title is not None else record().title,
                     authors=authors if authors is not None else record().authors,
                     year=year, ids=ids or record().ids, raw_fields=raw_fields or {}, pages=pages)
    pipe = AuditPipeline(AuditConfig(_env_file=None, model="test", use_llm=llm is not None,
                                     check_fields=check_fields, PARTIAL_AUTHORS=partial_authors), cache=cache,
                         adapters=[source], llm=llm)
    try:
        return await pipe._audit_all(AuditReport(entries=[EntryAudit(entry=entry)]), progress=False)
    finally:
        await pipe.aclose()


# Scenario: cpt-referenceaudit-algo-identification-contradiction
@pytest.mark.parametrize("check_fields", [True, False])
async def test_wrong_doi_target_overrides_metadata_match_and_is_rendered(tmp_path, check_fields):
    other = record(title="Motion-from-Blur", authors=["Denys Rozumnyi"],
                   ids=Identifiers(doi="10.1109/cvpr52688.2022.01552"))
    report = await run(tmp_path, Source([other], [record()]), ids=other.ids,
                       check_fields=check_fields)
    (audit,) = report.entries
    assert audit.verdict.kind == "contradiction"
    assert audit.verdict.artifacts[0].best_record.title == other.title
    assert any(f.field == "author" and f.status == "error" for f in audit.field_findings)
    assert report.summary["verdicts"]["contradiction"] == 1
    text = render_text(report)
    assert "CONTRADICTION" in text and other.title in text
    assert "1 contradictions" in text
    assert json.loads(render_json(report))["entries"][0]["verdict"]["kind"] == "contradiction"
    assert hb.predict(hb.compact(audit), "strict").label == "HALLUCINATED"
    assert hb.predict(hb.compact(audit), "identity").label == "UNCERTAIN"


async def test_contradiction_evidence_survives_verdict_cache(tmp_path):
    cache = AuditCache(tmp_path / "cache.db", model="test")
    other = record(authors=["Someone Else"])
    try:
        first = await run(tmp_path, Source([other]), cache=cache)
        second = await run(tmp_path, Source(error="source unavailable"), cache=cache)
    finally:
        cache.close()
    a, b = first.entries[0], second.entries[0]
    assert a.verdict.kind == b.verdict.kind == "contradiction"
    assert b.from_cache
    assert a.field_findings == b.field_findings
    assert a.issues == b.issues


@pytest.mark.parametrize("source", [Source(), Source(error="HTTP 503"),
                                    Source(search=[record(authors=["Someone Else"])])])
async def test_no_resolved_id_target_never_establishes_contradiction(tmp_path, source):
    report = await run(tmp_path, source)
    audit = report.entries[0]
    assert audit.verdict is None or audit.verdict.kind != "contradiction"


@pytest.mark.parametrize("changes", [{}, {"title": "MASKED AUTOENCODERS ARE SCALABLE VISION LEARNERS"},
                                    {"year": 2023}, {"authors": []}, {"title": ""}])
async def test_missing_cosmetic_and_uncertain_fields_are_not_contradictions(tmp_path, changes):
    report = await run(tmp_path, Source([record(**changes)]))
    audit = report.entries[0]
    assert audit.verdict is None or audit.verdict.kind != "contradiction"


async def test_definitively_wrong_year_is_a_contradiction(tmp_path):
    report = await run(tmp_path, Source([record()]), year=2035)
    assert report.entries[0].verdict.kind == "contradiction"
    assert any(f.field == "year" and f.status == "error"
               for f in report.entries[0].field_findings)


class TitleLLM:
    async def structured(self, system, user, schema, kind):
        if schema is CanCorrespondResult:
            return CanCorrespondResult(can_correspond=True, confidence="high", reason="by ID")
        assert schema is FieldJudgment
        return FieldJudgment(classification="error", confidence="high", reason="different title")

    async def aclose(self):
        pass


async def test_title_only_conflict_is_checked_against_the_resolved_paper(tmp_path):
    # Correspondence already auto-accepts this near-title match; the field check exposes the error.
    report = await run(tmp_path, Source([record()]),
                       title="Masked Autoencoders Are Scalable Image Learners", llm=TitleLLM())
    assert report.entries[0].verdict.kind == "contradiction"
    assert any(f.field == "title" and f.status == "error"
               for f in report.entries[0].field_findings)


async def test_an_inconclusive_id_check_is_reported_and_not_verdict_cached(tmp_path):
    cache = AuditCache(tmp_path / "cache.db", model="test")
    title = record().title + ": An Analysis"  # near-exact, auto-accepted
    try:
        first = await run(tmp_path, Source([record()]), cache=cache, title=title, check_fields=False)
        assert first.entries[0].verdict.kind == "exactly_one"
        assert any("consistency check incomplete" in i for i in first.entries[0].issues)
        assert cache.get_entry_verdict(first.entries[0].entry.content_hash) is None
        second = await run(tmp_path, Source([record()]), cache=cache, title=title,
                           check_fields=False, llm=TitleLLM())
        assert not second.entries[0].from_cache
        assert second.entries[0].verdict.kind == "contradiction"
    finally:
        cache.close()


@pytest.mark.parametrize("value", ["", "none", "N/A", "?"])
async def test_placeholder_fields_do_not_establish_an_id_metadata_conflict(tmp_path, value):
    report = await run(tmp_path, Source([record()]), raw_fields={"volume": value})
    assert report.entries[0].verdict.kind == "exactly_one"


async def test_paper_records_are_independent_when_one_id_check_fails(tmp_path):
    class FailingLLM(TitleLLM):
        async def structured(self, *args, **kwargs):
            raise RuntimeError("broken field checker")

    good = BibEntry(key="good", title=record().title, authors=record().authors, ids=record().ids)
    bad = good.model_copy(update={"key": "bad", "title": "Different title"})
    pipe = AuditPipeline(AuditConfig(_env_file=None, model="test"),
                         adapters=[Source([record()])], llm=FailingLLM())
    try:
        report = await pipe._audit_all(AuditReport(entries=[EntryAudit(entry=bad), EntryAudit(entry=good)]),
                                       progress=False)
    finally:
        await pipe.aclose()
    assert report.entries[0].verdict is None and report.entries[0].unresolved_reasons
    assert report.entries[1].verdict.kind == "exactly_one"


async def test_linked_preprint_and_publication_use_the_cited_version(tmp_path):
    preprint = record(source="arxiv", is_preprint=True, year=2020,
                      ids=Identifiers(arxiv_id="2001.12345", doi="10.48550/arxiv.2001.12345"))
    published = record(year=2024, venue="CVPR")
    report = await run(tmp_path, Source([published, preprint]), year=2024,
                       ids=Identifiers(doi=published.ids.doi, arxiv_id=preprint.ids.arxiv_id))
    assert report.entries[0].verdict.kind == "exactly_one"


async def test_fixing_a_conflicting_field_invalidates_the_entry_cache(tmp_path):
    cache = AuditCache(tmp_path / "cache.db", model="test")
    target = record(volume="3")
    try:
        bad = await run(tmp_path, Source([target]), cache=cache, raw_fields={"volume": "99"})
        assert bad.entries[0].verdict.kind == "contradiction"
        fixed = await run(tmp_path, Source([target]), cache=cache, raw_fields={"volume": "3"})
        assert not fixed.entries[0].from_cache
        assert fixed.entries[0].verdict.kind == "exactly_one"
        bad_again = await run(tmp_path, Source([target]), cache=cache, raw_fields={"volume": "99"})
        assert bad_again.entries[0].from_cache
        assert bad_again.entries[0].verdict.kind == "contradiction"
    finally:
        cache.close()


async def test_failed_id_resolution_is_retried_after_a_metadata_match(tmp_path):
    cache = AuditCache(tmp_path / "cache.db", model="test")
    try:
        first = await run(tmp_path, Source(error="HTTP 503", search=[record()]),
                          cache=cache, year=2035)
        assert first.entries[0].verdict.kind == "exactly_one"
        assert any("HTTP 503" in i for i in first.entries[0].issues)
        assert cache.get_entry_verdict(first.entries[0].entry.content_hash) is None
        second = await run(tmp_path, Source([record()], [record()]), cache=cache, year=2035)
        assert not second.entries[0].from_cache
        assert second.entries[0].verdict.kind == "contradiction"
    finally:
        cache.close()


async def test_cli_can_fail_on_contradiction(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from reference_audit import cli

    report = await run(tmp_path, Source([record(authors=["Someone Else"])]))
    bib = tmp_path / "refs.bib"
    bib.write_text("@article{k,title={T}}", encoding="utf-8")
    tex = tmp_path / "paper.tex"
    tex.write_text("\\cite{k}", encoding="utf-8")
    monkeypatch.setattr(cli, "run_audit", lambda *args, **kwargs: report)
    result = CliRunner().invoke(cli.app, ["audit", str(tex), str(bib), "--fail-on", "contradiction"])
    assert result.exit_code == 1
    assert "CONTRADICTION" in result.output


async def test_cli_audits_a_lone_bib(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from reference_audit import cli

    report = await run(tmp_path, Source([record(authors=["Someone Else"])]))
    bib = tmp_path / "refs.bib"
    bib.write_text("@article{k,title={T}}", encoding="utf-8")
    calls = []
    monkeypatch.setattr(cli, "run_audit", lambda *args, **kwargs: calls.append(args) or report)
    result = CliRunner().invoke(cli.app, ["audit", str(bib), "--fail-on", "contradiction"])
    assert calls == [(None, bib)]
    assert result.exit_code == 1


@pytest.mark.parametrize("kind,source_name", [("doi", "crossref"), ("arxiv_id", "arxiv"),
                                              ("pmid", "semantic_scholar"), ("openalex", "openalex")])
async def test_supported_hard_paper_ids_produce_contradictions(tmp_path, kind, source_name):
    value = {"doi": record().ids.doi, "arxiv_id": "2001.12345", "pmid": "1234",
             "openalex": "W1234"}[kind]
    ids = Identifiers(**{kind: value})
    source = Source([record(ids=ids, authors=["Someone Else"])])
    source.name = source_name
    report = await run(tmp_path, source, ids=ids)
    assert report.entries[0].verdict.kind == "contradiction"
    assert f"{kind}:{value}" in report.entries[0].verdict.rationale


async def test_page_punctuation_changes_cannot_reuse_a_clean_verdict(tmp_path):
    cache = AuditCache(tmp_path / "cache.db", model="test")
    try:
        first = await run(tmp_path, Source([record(pages="1-10")]), cache=cache, pages="1-10")
        assert first.entries[0].verdict.kind == "exactly_one"
        second = await run(tmp_path, Source([record(pages="1-10")]), cache=cache, pages="1,10")
        assert not second.entries[0].from_cache
        assert second.entries[0].verdict.kind == "contradiction"
    finally:
        cache.close()


async def test_omitted_authors_are_field_issues_under_either_policy(tmp_path):
    cache = AuditCache(tmp_path / "cache.db", model="test")
    try:
        for policy in ("warn", "error"):
            report = await run(tmp_path, Source([record()]), cache=cache,
                               authors=[record().authors[0]], partial_authors=policy)
            audit = report.entries[0]
            assert audit.verdict.kind == "exactly_one"
            finding = next(f for f in audit.field_findings if f.field == "author")
            assert finding.status == ("error" if policy == "error" else "uncertain")
    finally:
        cache.close()
