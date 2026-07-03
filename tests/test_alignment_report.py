"""Step 6 (Citation Alignment): report rendering (text + JSON)."""

from __future__ import annotations

import json

from reference_audit.models import (
    AlignmentFinding,
    AuditReport,
    BibEntry,
    EntryAudit,
    Identifiers,
    MatchedArtifact,
    Verdict,
)
from reference_audit.report import render_json, render_text


def _matched():
    return Verdict(kind="exactly_one", confidence="high", rationale="x",
                   artifacts=[MatchedArtifact()])


def _entry(key, findings, issues=None):
    return EntryAudit(
        entry=BibEntry(key=key, entry_type="article", title=key, cited=True,
                       ids=Identifiers(doi="10.1/" + key)),
        verdict=_matched(),
        alignment_findings=findings,
        issues=issues or [],
    )


def _report(entries):
    r = AuditReport(entries=entries)
    r.summary = {"total_entries": len(entries), "cited": len(entries),
                 "verdicts": {"exactly_one": len(entries), "none": 0, "multiple": 0, "unresolved": 0}}
    return r


def test_text_shows_alignment_tally_and_not_in_abstract_line():
    e = _entry("a", [AlignmentFinding(key="a", context_text="Widgets converge.",
                                      status="not_in_abstract", detail="silent")])
    text = render_text(_report([e]))
    assert "citation alignment: 1 checked" in text
    assert "not-in-abstract" in text
    assert "not confirmed by the cited abstract" in text
    assert "Widgets converge." in text


def test_text_contradicted_flows_through_issue_and_tally():
    # The pipeline puts contradicted on issues; the finding still counts in the tally.
    e = _entry(
        "b",
        [AlignmentFinding(key="b", context_text="Widgets converge.", status="contradicted",
                          evidence_quote="diverge", detail="reversed")],
        issues=["citation may misrepresent the source: … — reversed"],
    )
    text = render_text(_report([e]))
    assert "1 contradicted" in text
    assert "misrepresent" in text          # shown via the entry issue


def test_json_carries_alignment_findings():
    e = _entry("c", [AlignmentFinding(key="c", context_text="X.", status="supported",
                                      evidence_quote="x", confidence="high", via_llm=True)])
    data = json.loads(render_json(_report([e])))
    af = data["entries"][0]["alignment_findings"]
    assert af[0]["status"] == "supported" and af[0]["via_llm"] is True


def test_no_alignment_no_tally_line():
    e = _entry("d", [])
    assert "citation alignment:" not in render_text(_report([e]))
