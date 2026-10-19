"""Positive contradictions and scoped absence; an unknown venue remains explicitly unverified."""

import pytest

from reference_audit.cache.store import AuditCache
from reference_audit.config import AuditConfig
from reference_audit.fieldcheck import deterministic_field_checks, resolve_field_findings
from reference_audit.matching.pool import pool_candidates
from reference_audit.models import (
    BibEntry, EntryAudit, FieldJudgment, Identifiers, MatchedArtifact, SourceRecord,
    VenueCatalogue, VenueCatalogueEntry, Verdict,
)
from reference_audit.pipeline import AuditPipeline
from reference_audit.report import render_text
from reference_audit.models import AuditReport


def _entry(**kwargs):
    return BibEntry(key="k", title="A Study of Cats", authors=["Alice Author"], year=2022,
                    venue="ICML", ids=Identifiers(doi="10.1234/cats"), **kwargs)


def _record(source="crossref", venue="ICLR", doi="10.1234/cats", **kwargs):
    return SourceRecord(source=source, title="A Study of Cats", authors=["Alice Author"],
                        year=2022, venue=venue, ids=Identifiers(doi=doi), **kwargs)


def _artifact(*records):
    return MatchedArtifact(records=list(records), versions=list(records), best_record=records[0])


class VenueLLM:
    def __init__(self, classification="error"):
        self.classification, self.users = classification, []

    async def structured(self, system, user, schema, kind):
        assert schema is FieldJudgment and kind == "field_check"
        assert "FIELD UNDER REVIEW: journal/venue" in user
        self.users.append(user)
        return schema(classification=self.classification, confidence="high", reason="different venue")

    async def aclose(self):
        pass


async def _venue(entry, artifact, llm):
    findings = await resolve_field_findings(entry, artifact, llm,
                                           AuditConfig(_env_file=None, use_llm=False), None)
    return next(f for f in findings if f.field == "journal/venue")


@pytest.mark.parametrize("source", ["crossref", "publisher"])
async def test_cited_published_artifact_has_a_positive_venue_contradiction(source):
    llm = VenueLLM()
    finding = await _venue(_entry(), _artifact(_record(source)), llm)
    assert finding.status == "error"
    assert finding.venue_evidence.kind == "specific_artifact"
    assert finding.venue_evidence.source_urls == ["https://doi.org/10.1234/cats"]
    assert "10.1234/cats" in finding.detail and "10.1234/cats" in llm.users[0]


@pytest.mark.parametrize("record", [
    _record(doi="10.1234/another-version"),
    _record("openalex"), _record("dblp"),
    _record(doi=None),
    _record(doi="10.48550/arxiv.2201.00001"),
])
async def test_an_llm_error_without_artifact_binding_stays_unverifiable(record):
    finding = await _venue(_entry(), _artifact(record), VenueLLM())
    assert finding.status == "unverifiable" and finding.venue_evidence is None
    assert "may have another version" in finding.detail


async def test_an_arxiv_doi_cannot_prove_nonpublication_elsewhere():
    entry = _entry().model_copy(update={"ids": Identifiers(doi="10.48550/arxiv.2201.00001")})
    finding = await _venue(entry, _artifact(_record(doi=entry.ids.doi)), VenueLLM())
    assert finding.status == "unverifiable"


async def test_only_repository_records_do_not_invoke_an_llm_to_invent_absence():
    llm = VenueLLM()
    entry = _entry().model_copy(update={"venue": "Symposium on Probabilistic Reasoning"})
    finding = await _venue(entry, _artifact(_record("openalex", "arXiv (Cornell University)")), llm)
    assert finding.status == "unverifiable" and llm.users == []
    assert "could not be confirmed" in finding.detail


async def test_a_backfilled_identifier_is_not_the_cited_artifact():
    entry = _entry().model_copy(update={"ids": Identifiers()})
    finding = await _venue(entry, _artifact(_record()), VenueLLM())
    assert finding.status == "unverifiable"


async def test_an_empty_registrant_venue_does_not_authorize_an_aggregators_venue():
    finding = await _venue(_entry(), _artifact(_record(venue=""), _record("openalex")), VenueLLM())
    assert finding.status == "unverifiable" and finding.venue_evidence is None
    assert "sources report 'ICLR'" in finding.detail


async def test_cited_doi_container_overrides_another_versions_majority():
    records = [_record(), _record("openalex", "ICML", doi="10.1234/later"),
               _record("semantic_scholar", "ICML", doi="10.1234/later")]
    finding = await _venue(_entry(), _artifact(*records), VenueLLM())
    assert finding.status == "error" and finding.canonical_value == "ICLR"


async def test_a_pooled_preprint_doi_does_not_erase_leaf_publication_evidence():
    entry = _entry()
    records = [_record(), _record("openalex", "arXiv", doi="10.48550/arxiv.2201.00001",
                                 is_preprint=True, citation_count=100)]
    (pooled,) = pool_candidates(records)
    assert pooled.ids.doi.startswith("10.48550/arxiv")
    finding = await _venue(entry, _artifact(pooled), VenueLLM())
    assert finding.status == "error" and finding.venue_evidence.kind == "specific_artifact"


def _catalogue(**changes):
    base = dict(venue="ICML main conference", year=2022, scope="main_conference",
                source_url="https://proceedings.example/2022",
                completeness_url="https://proceedings.example/coverage",
                expected_count=1, complete=True,
                entries=[VenueCatalogueEntry(title="Learning about Dogs", authors=["Bob Other"])])
    return VenueCatalogue(**(base | changes))


def test_complete_catalogue_absence_is_scoped_and_needs_no_llm():
    entry = _entry().model_copy(update={"venue": "ICML main conference"})
    record = _record("openalex", "arXiv", venue_catalogues=[_catalogue()])
    finding = next(c.finding() for c in deterministic_field_checks(entry, _artifact(record))
                   if c.field == "journal/venue")
    assert finding.status == "error" and not finding.via_llm
    assert finding.venue_evidence.kind == "complete_catalogue"
    assert "2022, main_conference" in finding.venue_evidence.scope
    assert "scope only" in finding.detail


@pytest.mark.parametrize("catalogue, venue", [
    (_catalogue(complete=False), "ICML main conference"),
    (_catalogue(expected_count=2), "ICML main conference"),
    (_catalogue(year=2021), "ICML main conference"),
    (_catalogue(venue="ICML"), "ICML"),  # bare conference name may mean a workshop
    (_catalogue(venue="Proceedings of ICML Workshops"), "Proceedings of ICML Workshops"),
    (_catalogue(source_url="not a URL"), "ICML main conference"),
    (_catalogue(expected_count=2, entries=_catalogue().entries * 2), "ICML main conference"),
    (_catalogue(entries=[VenueCatalogueEntry(title="A Study of Cats", authors=["Other Author"])]),
     "ICML main conference"),
    (_catalogue(entries=[VenueCatalogueEntry(title="A Renamed Paper", authors=["Alice Author"])]),
     "ICML main conference"),
    (_catalogue(entries=[VenueCatalogueEntry(title="Different Title", authors=["Other Author"],
                                            ids=Identifiers(doi="10.1234/cats"))]),
     "ICML main conference"),
    (_catalogue(entries=[VenueCatalogueEntry(title="Different Title", authors=["Bob Other", "Alice Author"])]),
     "ICML main conference"),
])
def test_incomplete_wrong_scope_or_possible_identity_match_cannot_prove_absence(catalogue, venue):
    entry = _entry().model_copy(update={"venue": venue})
    record = _record("openalex", "arXiv", venue_catalogues=[catalogue])
    finding = next(c.finding() for c in deterministic_field_checks(entry, _artifact(record))
                   if c.field == "journal/venue")
    assert finding.status == "unverifiable" and finding.venue_evidence is None


def test_a_known_title_variant_in_a_catalogue_blocks_absence():
    entry = _entry().model_copy(update={"venue": "ICML main conference"})
    catalogue = _catalogue(entries=[VenueCatalogueEntry(title="Animal Cognition", authors=["Bob Other"])])
    record = _record("openalex", "arXiv", venue_catalogues=[catalogue])
    variant = _record("arxiv", "arXiv").model_copy(update={"title": "Animal Cognition"})
    finding = next(c.finding() for c in deterministic_field_checks(entry, _artifact(record, variant))
                   if c.field == "journal/venue")
    assert finding.status == "unverifiable"


def test_an_arxiv_version_identifier_in_a_catalogue_blocks_absence():
    entry = _entry().model_copy(update={"venue": "ICML main conference",
                                        "ids": Identifiers(arxiv_id="2201.00001v1")})
    catalogue = _catalogue(entries=[VenueCatalogueEntry(
        title="Different Title", authors=["Bob Other"],
        ids=Identifiers(doi="10.48550/arxiv.2201.00001v2"),
    )])
    record = _record("openalex", "arXiv", venue_catalogues=[catalogue])
    finding = next(c.finding() for c in deterministic_field_checks(entry, _artifact(record))
                   if c.field == "journal/venue")
    assert finding.status == "unverifiable"


async def test_unverified_venue_keeps_identity_and_reports_its_actual_reason(tmp_path):
    entry, artifact = _entry(), _artifact(_record("dblp"))
    verdict = Verdict(kind="exactly_one", artifacts=[artifact], confidence="high")
    audit = EntryAudit(entry=entry, verdict=verdict)
    pipe = AuditPipeline(AuditConfig(_env_file=None, use_llm=False), adapters=[], llm=VenueLLM())
    try:
        await pipe._check_fields(audit, verdict)
    finally:
        await pipe.aclose()
    assert audit.verdict is verdict
    venue = next(f for f in audit.field_findings if f.field == "journal/venue")
    assert venue.status == "unverifiable" and any(venue.detail in issue for issue in audit.issues)
    text = render_text(AuditReport(entries=[audit]))
    assert "may have another version" in text
    cache = AuditCache(tmp_path / "cache.db", pipeline_version="0.26", model="t")
    try:
        cache.put_entry_verdict(entry.content_hash, verdict)
        cache.pipeline_version = AuditConfig(_env_file=None).pipeline_version
        assert cache.pipeline_version == "0.30" and cache.get_entry_verdict(entry.content_hash) is None
    finally:
        cache.close()
