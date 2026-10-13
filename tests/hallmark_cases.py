"""Pinned public HALLMARK cases and evidence-backed local expectations; no HALLMARK import."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

import hallmark_bench as hb

DATA = Path(__file__).parent / "fixtures" / "hallmark" / "v1.2"
SPLITS = ("dev_public", "test_public")
Label = Literal["VALID", "HALLUCINATED"]

# These inputs are damaged in upstream v1.2. They must remain explicit non-measurements;
# neither their VALID nor their HALLUCINATED label is an oracle for parsing success.
UNPARSEABLE = {
    "dev_public": {
        "c080618bff76", "e7b8d9a1670b", "c74be625b875", "a687f76f3a21",
        "dae1eb71d49a", "aaefe29933ae", "fdcf8e3071b7",
    },
    "test_public": {"f746e1c10ae9", "e65a9f529e01"},
}


class PublicRecord(hb.HallmarkRecord):
    label: Label
    hallucination_type: str | None = None
    difficulty_tier: int | None = None


class LabelReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    split: Literal["dev_public", "test_public"]
    bibtex_key: str
    upstream_label: Label
    expected_label: Label | None
    disposition: Literal["correction", "disputed"]
    reason: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    error_field: str | None = None

    @model_validator(mode="after")
    def require_review_evidence(self):
        if not self.reason.strip() or any(not e.strip() for e in self.evidence):
            raise ValueError("a label review requires a reason and evidence")
        if self.disposition == "correction":
            if self.expected_label is None or self.expected_label == self.upstream_label:
                raise ValueError("a correction must change the upstream label")
            if not self.error_field:
                raise ValueError("a correction must name the independently checked field")
        elif self.expected_label is not None:
            raise ValueError("a disputed label must have no guessed expectation")
        return self


@dataclass(frozen=True)
class Case:
    split: str
    record: hb.HallmarkRecord
    upstream_label: Label
    reviewed_benchmark_label: Label | None
    review: LabelReview | None
    native_expectation: NativeExpectation | None = None

    @property
    def id(self) -> str:
        return f"{self.split}/{self.record.bibtex_key}"


class NativeExpectation(BaseModel):
    """Independent expectations in reference-audit's vocabulary, never inferred from a label."""

    model_config = ConfigDict(extra="forbid")
    split: Literal["dev_public", "test_public"]
    bibtex_key: str
    identity: Literal["none", "exactly_one", "contradiction"] | None = None
    fields: dict[str, list[Literal["ok", "formatting", "error", "uncertain", "unverifiable"]]]
    reason: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def require_independent_basis(self):
        if not self.reason.strip() or any(not e.strip() for e in self.evidence):
            raise ValueError("a native expectation requires a reason and independent evidence")
        if self.identity is None and not self.fields:
            raise ValueError("a native expectation must constrain identity or metadata")
        if any(not field.strip() or not statuses for field, statuses in self.fields.items()):
            raise ValueError("a native metadata expectation requires a field and allowed statuses")
        return self


def assert_native_outcome(case: Case, audit: hb.CompactAudit) -> None:
    """Check the auditor's contract. Explicit failure/ambiguity is an outcome, never a guessed label."""
    assert audit.key == case.record.bibtex_key
    if audit.status != "audited":
        assert audit.verdict is None and audit.failure.strip(), case.id
        if audit.status == "unparsed":
            assert audit.key in UNPARSEABLE[case.split], audit.failure
        return
    if audit.verdict is None:
        assert audit.unresolved_reasons and all(r.strip() for r in audit.unresolved_reasons), case.id
        assert audit.issues and not audit.from_cache, case.id
        return
    assert audit.rationale.strip(), case.id
    if audit.verdict == "multiple":
        return  # a distinct, reported identification result; not a binary classification miss
    expected = case.native_expectation
    if expected and expected.identity:
        assert audit.verdict == expected.identity, f"{case.id}: {expected.reason}; {audit.rationale}"
    if audit.verdict == "none":
        assert not audit.findings, "metadata checks must not claim an identified artifact"
        return
    assert audit.matched_title.strip() and audit.matched_source.strip(), case.id
    if audit.verdict == "contradiction":
        assert any(f.status == "error" for f in audit.findings), (
            f"{case.id}: contradiction has no substantive field evidence"
        )
        assert audit.issues, f"{case.id}: contradiction was not reported"
    for finding in audit.findings:
        if finding.status in {"error", "uncertain", "unverifiable"}:
            assert finding.detail.strip(), f"{case.id}: {finding.field} has no reason"
            assert any(finding.field in issue for issue in audit.issues), (
                f"{case.id}: {finding.field} finding was not reported"
            )
        if finding.field == "journal/venue" and finding.status == "error":
            assert finding.venue_evidence is not None, f"{case.id}: venue error has no bounded proof"
    if expected:
        for field, statuses in expected.fields.items():
            findings = [f for f in audit.findings if f.field == field]
            assert len(findings) == 1 and findings[0].status in statuses, (
                f"{case.id}: expected {field} in {statuses}; {expected.reason}; got {findings}"
            )


def load_cases(data: Path = DATA) -> list[Case]:
    reviews = [LabelReview.model_validate(r) for r in
               json.loads((data / "label_reviews.json").read_text(encoding="utf-8"))]
    by_review = {(r.split, r.bibtex_key): r for r in reviews}
    if len(by_review) != len(reviews):
        raise ValueError("duplicate HALLMARK label review")
    seen = set()
    native = [NativeExpectation.model_validate(r) for r in
              json.loads((data / "native_expectations.json").read_text(encoding="utf-8"))]
    by_native = {(r.split, r.bibtex_key): r for r in native}
    if len(by_native) != len(native):
        raise ValueError("duplicate native HALLMARK expectation")
    cases = []
    for split in SPLITS:
        for line in (data / f"{split}.jsonl").read_text(encoding="utf-8").splitlines():
            row = PublicRecord.model_validate_json(line)
            key = (split, row.bibtex_key)
            if key in seen:
                raise ValueError(f"duplicate HALLMARK record: {key}")
            seen.add(key)
            review = by_review.get(key)
            if review and review.upstream_label != row.label:
                raise ValueError(f"stale HALLMARK label review: {key}")
            # Strip labels, explanations, generation type and relabel history before auditing.
            blind = hb.HallmarkRecord(
                bibtex_key=row.bibtex_key, bibtex_type=row.bibtex_type, fields=dict(row.fields),
            )
            cases.append(Case(split, blind, row.label,
                              review.expected_label if review else row.label, review, by_native.get(key)))
    if orphaned := by_review.keys() - seen:
        raise ValueError(f"orphaned HALLMARK label review(s): {sorted(orphaned)}")
    if orphaned := by_native.keys() - seen:
        raise ValueError(f"orphaned native HALLMARK expectation(s): {sorted(orphaned)}")
    return cases


CASES = load_cases()
