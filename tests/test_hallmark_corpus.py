"""Every copied public example exercises real parsing; corrected labels have source regressions."""

import hashlib
import json
from collections import Counter

import pytest

import hallmark_bench as hb
from hallmark_cases import CASES, DATA, SPLITS, UNPARSEABLE, LabelReview, load_cases
from reference_audit.config import AuditConfig
from reference_audit.fieldcheck import deterministic_field_checks, resolve_field_findings
from reference_audit.models import (
    EntryAudit, EntryType, FieldJudgment, MatchedArtifact, SourceQueryResult, SourceRecord, Verdict,
)
from reference_audit.parsing.bib import parse_bib
from reference_audit.pipeline import AuditPipeline, build_parse_report
from reference_audit.sources.base import SourceAdapter


def test_copied_data_matches_pinned_provenance():
    manifest = json.loads((DATA / "provenance.json").read_text())
    assert manifest["commit"] == "f774fa40675daa83eca6201637a94c4536b7bb3e"
    assert manifest["version"] == "v1.2"
    for name, info in manifest["files"].items():
        data = (DATA / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == info["sha256"], name
        if "records" in info:
            assert len(data.splitlines()) == info["records"]
    assert Counter(c.split for c in CASES) == {"dev_public": 1119, "test_public": 831}
    assert Counter((c.split, c.upstream_label) for c in CASES) == {
        ("dev_public", "VALID"): 513, ("dev_public", "HALLUCINATED"): 606,
        ("test_public", "VALID"): 312, ("test_public", "HALLUCINATED"): 519,
    }
    for split in SPLITS:
        assert UNPARSEABLE[split] <= {c.record.bibtex_key for c in CASES if c.split == split}
    assert all(set(c.record.model_dump()) == {"bibtex_key", "bibtex_type", "fields"}
               for c in CASES)


@pytest.fixture(scope="module", params=SPLITS)
def parsed_split(request, tmp_path_factory):
    split = request.param
    workdir = tmp_path_factory.mktemp(split)
    cases = [c for c in CASES if c.split == split]
    intact, broken = hb.round_trip([c.record for c in cases], workdir)
    assert set(broken) == UNPARSEABLE[split]
    assert {r.bibtex_key for r in intact} | set(broken) == {c.record.bibtex_key for c in cases}
    report = build_parse_report(None, workdir / "preflight.bib")
    assert {u.key for u in report.unparsed} == set(broken)
    assert len(report.entries) + len(report.unparsed) == len(cases)
    return split, {a.entry.key: a for a in report.entries}, broken


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_public_example_parsing(case, tmp_path):
    """Parse records independently too: the last malformed entry has no following block to help it."""
    hb.write_bib([case.record], tmp_path / "entry.bib")
    entries, twins, unparsed = parse_bib(tmp_path / "entry.bib")
    assert twins == []
    if case.record.bibtex_key in UNPARSEABLE[case.split]:
        assert entries == []
        assert len(unparsed) == 1 and unparsed[0].key == case.record.bibtex_key
        assert "unbalanced braces" in unparsed[0].reason
    else:
        assert unparsed == []
        assert len(entries) == 1 and entries[0].key == case.record.bibtex_key
        assert entries[0].title
        intact, broken = hb.round_trip([case.record], tmp_path)
        assert intact == [case.record] and broken == {}


def test_malformed_records_do_not_hide_their_neighbours(parsed_split):
    split, parsed, broken = parsed_split
    for key, reason in broken.items():
        assert "unbalanced braces" in reason
        assert key not in parsed
        prediction = hb.predict(hb.not_audited(key, "unparsed", reason), "strict")
        assert (prediction.label, prediction.evaluated) == ("UNCERTAIN", False)
        assert reason in prediction.reason
    assert set(parsed) == {
        c.record.bibtex_key for c in CASES if c.split == split
    } - UNPARSEABLE[split]


def test_reviewed_labels_are_separate_from_unchanged_upstream_data():
    corrections = [c for c in CASES if c.review and c.review.disposition == "correction"]
    assert Counter(c.split for c in corrections) == {"dev_public": 8, "test_public": 4}
    assert all(c.upstream_label == "VALID" and c.reviewed_benchmark_label == "HALLUCINATED"
               for c in corrections)
    disputed = [c for c in CASES if c.review and c.review.disposition == "disputed"]
    assert [c.record.bibtex_key for c in disputed] == ["a0527a7c2d1b"]
    assert disputed[0].reviewed_benchmark_label is None
    assert all(c.reviewed_benchmark_label == c.upstream_label for c in CASES if c.review is None)


@pytest.mark.parametrize("mutation, why", [
    ("duplicate", "duplicate HALLMARK label review"),
    ("orphan", "orphaned HALLMARK label review"),
    ("stale", "stale HALLMARK label review"),
])
def test_label_review_drift_fails_loudly(tmp_path, mutation, why):
    for split in SPLITS:
        (tmp_path / f"{split}.jsonl").write_bytes((DATA / f"{split}.jsonl").read_bytes())
    (tmp_path / "native_expectations.json").write_bytes((DATA / "native_expectations.json").read_bytes())
    reviews = json.loads((DATA / "label_reviews.json").read_text())
    if mutation == "duplicate":
        reviews.append(reviews[0])
    elif mutation == "orphan":
        reviews[0]["bibtex_key"] = "missing"
    else:
        reviews[0].update(upstream_label="HALLUCINATED", expected_label="VALID")
    (tmp_path / "label_reviews.json").write_text(json.dumps(reviews))
    with pytest.raises(ValueError, match=why):
        load_cases(tmp_path)


def test_dispute_cannot_be_given_a_guessed_label():
    review = next(c.review for c in CASES if c.reviewed_benchmark_label is None)
    with pytest.raises(ValueError, match="no guessed expectation"):
        LabelReview.model_validate(review.model_dump() | {"expected_label": "VALID"})


class RecordedCrossref(SourceAdapter):
    name = "crossref"
    handles = set(EntryType)

    def __init__(self, record):
        # A real HTTP client is deliberately absent: an unrecorded lookup is a test failure.
        self.record = record

    async def lookup_by_id(self, ids):
        assert ids.doi == self.record.ids.doi
        return SourceQueryResult(source=self.name, query_kind="id", records=[self.record])

    async def search_by_metadata(self, entry, limit=10):
        raise AssertionError("this field-check regression must not search by metadata")

    async def aclose(self):
        pass


class ReviewedVenueLLM:
    """Replay the venue judgment supported by the independently recorded publication."""

    def __init__(self, entry, source, review):
        self.entry, self.source, self.review = entry, source, review
        self.calls = 0

    async def structured(self, system, user, schema, kind):
        assert schema is FieldJudgment and kind == "field_check"
        assert "FIELD UNDER REVIEW: journal/venue\n" in user
        assert f"citation (.bib) value:  {self.entry.venue}\n" in user
        assert f"authoritative value:    {self.source.venue}" in user
        assert f"title:   {self.source.title}" in user
        self.calls += 1
        return FieldJudgment(classification="error", confidence="high", reason=self.review.reason)


@pytest.mark.parametrize("case", [c for c in CASES if c.review and
                                 c.review.disposition == "correction"], ids=lambda c: c.id)
async def test_label_correction_against_recorded_independent_sources(case, tmp_path):
    """A benchmark VALID label must not teach the checker to accept a wrong DOI/author/venue."""
    snapshots = json.loads((DATA / "recorded_sources.json").read_text())
    assert set(snapshots) == {c.record.bibtex_key for c in CASES if c.review and
                             c.review.disposition == "correction"}
    snapshot = snapshots[case.record.bibtex_key]
    assert snapshot["recorded_at"] and snapshot["evidence_url"] in case.review.evidence
    source = SourceRecord.model_validate(snapshot["record"])
    artifact = MatchedArtifact(records=[source], versions=[source], best_record=source,
                               merged_ids=source.ids)
    hb.write_bib([case.record], tmp_path / "entry.bib")
    (entry,), twins, unparsed = parse_bib(tmp_path / "entry.bib")
    assert twins == unparsed == []
    checks = deterministic_field_checks(entry, artifact, partial_authors="error")
    findings = [c.finding() for c in checks if not c.needs_llm]
    if case.review.error_field == "journal/venue":
        llm = ReviewedVenueLLM(entry, source, case.review)
        config = AuditConfig(_env_file=None).model_copy(update={"partial_authors": "error"})
        findings = await resolve_field_findings(
            entry, artifact, llm, config, None,
            skip_fields=frozenset(c.field for c in checks if c.field != "journal/venue"),
        )
        assert llm.calls == 1
    if case.review.error_field == "doi":
        other = SourceRecord.model_validate(snapshot["cited_doi_record"])
        pipe = AuditPipeline(AuditConfig(_env_file=None, use_llm=False),
                             adapters=[RecordedCrossref(other)])
        try:
            finding = await pipe._check_cited_doi(entry, artifact)
        finally:
            await pipe.aclose()
        assert other.title in finding.detail
        findings.append(finding)
    expected_status = "unverifiable" if case.review.error_field == "journal/venue" else "error"
    assert any(f.field == case.review.error_field and f.status == expected_status for f in findings)
    audit = EntryAudit(entry=entry, field_findings=findings,
                       verdict=Verdict(kind="exactly_one", artifacts=[artifact], confidence="high"))
    assert audit.verdict.kind == "exactly_one"
