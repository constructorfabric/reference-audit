"""Step 1 (Citation Alignment): domain models + config flag.

Pure, offline: exercises the new pydantic models and the opt-in config flag. The behavioural
funnel is covered from Step 4 onward with a mocked LLM.
"""

from __future__ import annotations

import pytest

from reference_audit.config import AuditConfig
from reference_audit.llm.schemas import strict_schema
from reference_audit.models import (
    AlignmentFinding,
    CitationAlignmentResult,
    CitationContext,
    EntryAudit,
    BibEntry,
    SourceRecord,
)


def test_source_record_carries_abstract():
    rec = SourceRecord(source="openalex", abstract="We show that X implies Y.")
    assert rec.abstract == "We show that X implies Y."
    # default is empty, never None (so downstream "no abstract" is an explicit empty check)
    assert SourceRecord(source="crossref").abstract == ""


def test_citation_context_roundtrip():
    ctx = CitationContext(key="smith2020", text="X improves Y.", ordinal=2, command="citep")
    assert (ctx.key, ctx.ordinal, ctx.command) == ("smith2020", 2, "citep")
    assert CitationContext.model_validate_json(ctx.model_dump_json()) == ctx
    # ordinal/command have sane defaults
    bare = CitationContext(key="k", text="t")
    assert (bare.ordinal, bare.command) == (0, "cite")


@pytest.mark.parametrize(
    "status", ["supported", "contradicted", "not_in_abstract", "unverifiable"]
)
def test_alignment_finding_all_statuses(status):
    f = AlignmentFinding(key="k", status=status, confidence="high")
    assert f.status == status
    assert AlignmentFinding.model_validate_json(f.model_dump_json()) == f


def test_alignment_result_llm_polarity_labels():
    # The LLM model may only return the three affirmative labels; 'unverifiable' is system-owned.
    for c in ("supported", "contradicted", "not_in_abstract"):
        r = CitationAlignmentResult(classification=c, confidence="medium", reason="…")
        assert r.classification == c
    with pytest.raises(ValueError):
        CitationAlignmentResult(classification="unverifiable", confidence="low", reason="x")


def test_alignment_result_strict_schema_is_openai_ready():
    schema = strict_schema(CitationAlignmentResult)
    assert schema["additionalProperties"] is False
    # strict mode requires every property listed as required, including the defaulted evidence_quote
    assert set(schema["required"]) == {"classification", "confidence", "reason", "evidence_quote"}


def test_entry_audit_alignment_findings_default_empty():
    audit = EntryAudit(entry=BibEntry(key="k"))
    assert audit.alignment_findings == []


def test_config_check_alignment_opt_in():
    # advisory + costly → off by default, unlike check_fields
    assert AuditConfig(_env_file=None).check_alignment is False
    assert AuditConfig(_env_file=None).check_fields is True
    assert AuditConfig(_env_file=None).model_copy(update={"check_alignment": True}).check_alignment
