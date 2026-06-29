"""Turn scored + clustered candidates into a 3-way Verdict (README 5.1/5.2/5.3).

`artifacts` is the SAME-OBJECT clustering of the *accepted* candidates (see `sameobject.py`): each
element is one distinct real-world work. The verdict counts them.
"""

from __future__ import annotations

from reference_audit.models import CandidateAssessment, MatchedArtifact, Verdict


# @cpt-dod:cpt-referenceaudit-dod-identification-three-way-verdict:p1
# @cpt-dod:cpt-referenceaudit-dod-identification-hallucination-screen:p1
def build_verdict(
    assessments: list[CandidateAssessment],
    artifacts: list[MatchedArtifact],
    *,
    errored: bool,
) -> Verdict | None:
    if artifacts:
        if len(artifacts) == 1:
            best = artifacts[0].best_record
            src = best.source if best else ""
            n_versions = len(artifacts[0].versions)
            extra = f" ({n_versions} versions)" if n_versions > 1 else ""
            return Verdict(
                kind="exactly_one",
                artifacts=artifacts,
                confidence="high",
                rationale=f"Matched a single work via {src}{extra}.",
            )
        return Verdict(
            kind="multiple",
            artifacts=artifacts,
            confidence="medium",
            rationale=f"Matched {len(artifacts)} distinct works.",
        )

    if errored:
        return None  # transient failure — leave unresolved, retry next run (error ≠ not-found)

    if not assessments:
        return Verdict(
            kind="none",
            confidence="high",
            rationale="No database record found for this reference (possible hallucination).",
        )

    if all(a.bucket == "auto_reject" for a in assessments):
        return Verdict(
            kind="none",
            confidence="medium",
            rationale="Database records were found but none matched the entry's title/authors.",
        )

    return None  # adjudicate bucket remaining — deferred to the LLM funnel
