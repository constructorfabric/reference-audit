"""Native corpus checks accept reported uncertainty and reject unreported or unsupported outcomes."""

from dataclasses import replace

import pytest

import hallmark_bench as hb
from hallmark_cases import CASES, NativeExpectation, assert_native_outcome
from reference_audit.models import FieldFinding

CASE = next(c for c in CASES if c.record.bibtex_key == "87a25c18d319")


def _one(**changes):
    return hb.CompactAudit(**(dict(key=CASE.record.bibtex_key, status="audited", verdict="exactly_one",
                                  rationale="matched by identifier", matched_source="arxiv",
                                  matched_title=CASE.record.fields["title"]) | changes))


def test_real_work_with_unverified_venue_does_not_have_to_reproduce_hallmark():
    assert CASE.upstream_label == "HALLUCINATED"
    finding = FieldFinding(field="journal/venue", status="unverifiable",
                           detail="Only the preprint could be found; the claimed venue is unverified")
    audit = _one(findings=[finding], issues=["journal/venue: " + finding.detail])
    assert hb.predict(audit, "strict").label == "UNCERTAIN"
    assert_native_outcome(CASE, audit)


@pytest.mark.parametrize("reason", ["arxiv id query failed: http 429",
                                     "3 candidates beyond the per-entry LLM cap were not adjudicated"])
def test_reported_unresolved_outcomes_are_accepted(reason):
    audit = hb.CompactAudit(key=CASE.record.bibtex_key, status="audited",
                            unresolved_reasons=[reason], issues=["unresolved: " + reason])
    assert_native_outcome(CASE, audit)


def test_bare_null_verdict_fails():
    with pytest.raises(AssertionError):
        assert_native_outcome(CASE, hb.CompactAudit(key=CASE.record.bibtex_key, status="audited"))


def test_multiple_is_a_reported_native_result():
    assert_native_outcome(CASE, _one(verdict="multiple", rationale="Matched 2 distinct works"))


def test_contradiction_requires_substantive_reported_evidence():
    case = replace(CASE, native_expectation=None)
    finding = FieldFinding(field="author", status="error", detail="ID belongs to other authors")
    audit = _one(verdict="contradiction", findings=[finding], issues=["author: " + finding.detail])
    assert_native_outcome(case, audit)
    with pytest.raises(AssertionError, match="no substantive field evidence"):
        assert_native_outcome(case, _one(verdict="contradiction"))


def test_a_failed_record_has_to_explain_why():
    assert_native_outcome(CASE, hb.not_audited(CASE.record.bibtex_key, "failed", "source error"))
    with pytest.raises(AssertionError):
        assert_native_outcome(CASE, hb.not_audited(CASE.record.bibtex_key, "failed", ""))


def test_an_llm_venue_error_without_scoped_proof_is_rejected():
    finding = FieldFinding(field="journal/venue", status="error", detail="No website found")
    with pytest.raises(AssertionError, match="bounded proof"):
        assert_native_outcome(CASE, _one(findings=[finding], issues=["journal/venue: No website found"]))


def test_unverified_metadata_cannot_be_silent():
    finding = FieldFinding(field="journal/venue", status="unverifiable", detail="Only a preprint")
    with pytest.raises(AssertionError, match="not reported"):
        assert_native_outcome(CASE, _one(findings=[finding]))


def test_independent_identity_expectations_still_detect_wrong_verdicts():
    expectation = NativeExpectation(split=CASE.split, bibtex_key=CASE.record.bibtex_key,
                                    identity="exactly_one", fields={}, reason="independent arXiv record",
                                    evidence=["https://arxiv.org/abs/2602.12271"])
    case = replace(CASE, native_expectation=expectation)
    with pytest.raises(AssertionError):
        assert_native_outcome(case, _one(verdict="none"))
