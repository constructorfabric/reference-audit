"""Render an AuditReport as JSON (machine, use-case c) or human-readable text."""

from __future__ import annotations

from reference_audit.models import AlignmentFinding, AuditReport, EntryAudit, FieldFinding


def render_json(report: AuditReport) -> str:
    return report.model_dump_json(indent=2)


def _ids_str(audit: EntryAudit) -> str:
    ids = audit.entry.ids
    parts = []
    if ids.doi:
        parts.append(f"doi:{ids.doi}")
    if ids.arxiv_id:
        parts.append(f"arXiv:{ids.arxiv_id}")
    if ids.isbn13:
        parts.append(f"isbn:{ids.isbn13}")
    if ids.openalex:
        parts.append(f"openalex:{ids.openalex}")
    if ids.url and not ids.doi:
        parts.append(f"url:{ids.url}")
    return "  ".join(parts) if parts else "(no identifier)"


_VERDICT_GLYPH = {"exactly_one": "✓", "none": "✗", "multiple": "?"}


def _verdict_line(audit) -> str | None:
    v = audit.verdict
    if v is None:
        return None
    glyph = _VERDICT_GLYPH.get(v.kind, "·")
    label = {"exactly_one": "exactly one match", "none": "NO MATCH (possible hallucination)",
             "multiple": "MULTIPLE matches"}.get(v.kind, v.kind)
    line = f"    {glyph} {label} ({v.confidence}) — {v.rationale}"
    if v.kind == "exactly_one" and v.artifacts and v.artifacts[0].best_record:
        br = v.artifacts[0].best_record
        if br.ids.doi:
            line += f"\n      matched: doi:{br.ids.doi}  ({br.source})"
        elif br.ids.isbn13:
            line += f"\n      matched: isbn:{br.ids.isbn13}  ({br.source})"
        elif br.ids.openalex:
            line += f"\n      matched: openalex:{br.ids.openalex}  ({br.source})"
        elif br.ids.url:
            line += f"\n      matched: url:{br.ids.url}  ({br.source})"
    return line


def _nit_line(f: FieldFinding) -> str:
    """One formatting nit (a cosmetic, not-a-mistake field difference) spelled out."""
    tag = "(LLM) " if f.via_llm else ""
    src = f" [{', '.join(f.sources)}]" if f.sources else ""
    return f"    · {tag}formatting nit in '{f.field}'='{f.bib_value}' — {f.detail}{src}"


def _formatting_nits(a: EntryAudit) -> list[FieldFinding]:
    return [f for f in a.field_findings if f.status == "formatting"]


def _alignment_advisories(a: EntryAudit) -> list[AlignmentFinding]:
    """Advisory alignment notes shown inline (not_in_abstract). Contradicted and unverifiable are
    surfaced as entry issues by the pipeline; supported findings are machine-only (JSON)."""
    return [f for f in a.alignment_findings if f.status == "not_in_abstract"]


def _alignment_line(f: AlignmentFinding) -> str:
    where = f" #{f.ordinal + 1}" if f.ordinal else ""
    return (
        f"    · citation{where} not confirmed by the cited abstract "
        f"(claim: “{f.context_text}”) — {f.detail}"
    )


def _has_issues(a: EntryAudit, *, network: bool) -> bool:
    """Does this entry need attention? An issue, or (once the network ran) a verdict that is not a
    clean single match — a possible hallucination, an ambiguous multi-match, or an unresolved entry
    we could not check. Formatting nits alone do NOT count as issues (they are cosmetic)."""
    if a.issues:
        return True
    if network:
        v = a.verdict
        if v is None or v.kind in ("none", "multiple"):
            return True
    return False


def _entry_block(a: EntryAudit) -> list[str]:
    """The full report block for one entry: header, ids, issues, formatting nits, verdict."""
    e = a.entry
    flag = "cited" if e.cited else "UNCITED"
    block = [
        f"[{e.entry_type.value}] {e.key}  ({flag})",
        f"    {e.title or '(no title)'}",
        f"    ids: {_ids_str(a)}",
    ]
    for issue in a.issues:
        block.append(f"    ⚠ {issue}")
    for f in _formatting_nits(a):
        block.append(_nit_line(f))
    for f in _alignment_advisories(a):
        block.append(_alignment_line(f))
    vline = _verdict_line(a)
    if vline:
        block.append(vline)
    return block


def _category_section(bucket: list[EntryAudit], heading: str, empty_message: str) -> list[str]:
    """A named report category: the full entry block for each member, or — when none qualify — a
    single explicit line saying so (a clean run states it plainly rather than going silent)."""
    if not bucket:
        return [empty_message, ""]
    out = [heading.format(n=len(bucket)), ""]
    for a in bucket:
        out.extend(_entry_block(a))
        out.append("")
    return out


def render_text(report: AuditReport) -> str:
    s = report.summary
    verdicts = s.get("verdicts")
    lines: list[str] = []
    header = "Reference audit" if verdicts else "Reference audit — parse summary (no network)"
    lines.append(header)
    lines.append(
        f"  {s.get('total_entries', 0)} entries"
        f"  ·  {s.get('cited', 0)} cited"
        f"  ·  {s.get('uncited', 0)} uncited"
        f"  ·  {s.get('entries_with_issues', 0)} with issues"
        f"  ·  {s.get('commented_twins', 0)} commented twins"
    )
    if verdicts:
        lines.append(
            f"  verdicts: {verdicts.get('exactly_one', 0)} matched"
            f"  ·  {verdicts.get('none', 0)} no-match"
            f"  ·  {verdicts.get('multiple', 0)} ambiguous"
            f"  ·  {verdicts.get('unresolved', 0)} unresolved"
        )
    align = [f for a in report.entries for f in a.alignment_findings]
    if align:
        by_status: dict[str, int] = {}
        for f in align:
            by_status[f.status] = by_status.get(f.status, 0) + 1
        lines.append(
            f"  citation alignment: {len(align)} checked"
            f"  ·  {by_status.get('contradicted', 0)} contradicted"
            f"  ·  {by_status.get('supported', 0)} supported"
            f"  ·  {by_status.get('not_in_abstract', 0)} not-in-abstract"
            f"  ·  {by_status.get('unverifiable', 0)} unverifiable"
        )
    by_type = s.get("by_type", {})
    if by_type:
        lines.append("  types: " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))
    lines.append("")

    # Group entries so the reader sees the gravest first. The two headline categories lead, each its
    # own section: CAPITAL OFFENCES (conclusive hallucinations — verdict `none`, no real document
    # corresponds) and UNABLE TO VERIFY (we could not conclusively rule a hallucination out — the
    # verdict was left None by a transient network/LLM error, an unfamiliar .bib type, a dead link,
    # or an adjudication we could not settle). Both are verdict-aware, so they apply only once the
    # network has run; in --no-network mode there are no verdicts and every entry falls through to
    # the issue/nit/clean split. The remaining entries — at least one artifact positively identified
    # — are then split into issues, formatting-nits-only, and clean.
    network = verdicts is not None
    capital_offences: list[EntryAudit] = []
    unable_to_verify: list[EntryAudit] = []
    with_issues: list[EntryAudit] = []
    nits_only: list[EntryAudit] = []
    clean: list[EntryAudit] = []
    for a in report.entries:
        if network and a.verdict is not None and a.verdict.kind == "none":
            capital_offences.append(a)
        elif network and a.verdict is None:
            unable_to_verify.append(a)
        elif _has_issues(a, network=network):
            with_issues.append(a)
        elif _formatting_nits(a) or _alignment_advisories(a):
            nits_only.append(a)
        else:
            clean.append(a)

    if network:
        lines.extend(_category_section(
            capital_offences,
            "CAPITAL OFFENCES ({n}) — hallucinated citations (no real document corresponds):",
            "CAPITAL OFFENCES — No hallucinated citations",
        ))
        lines.extend(_category_section(
            unable_to_verify,
            "UNABLE TO VERIFY ({n}) — could not conclusively rule out a hallucination "
            "(network/LLM error, unfamiliar entry type, dead link, …):",
            "UNABLE TO VERIFY — For all other references at least one matching artifact "
            "was positively identified",
        ))

    groups = [
        (with_issues, "ISSUES ({n}) — other problems to review:"),
        (nits_only, "FORMATTING NITS & ADVISORIES ({n}) — cosmetic field fixes or citation notes:"),
        (clean, "NO ISSUES ({n}) — verified, nothing to fix:"),
    ]
    for bucket, heading in groups:
        if not bucket:
            continue
        lines.append(heading.format(n=len(bucket)))
        lines.append("")
        for a in bucket:
            lines.extend(_entry_block(a))
            lines.append("")

    if report.cited_but_missing:
        lines.append("CITED BUT MISSING FROM .bib (error — dangling citation):")
        for k in report.cited_but_missing:
            lines.append(f"    \\cite{{{k}}}  → no bib entry")
        lines.append("")

    if report.missing_includes:
        lines.append(
            "UNRESOLVED \\input/\\include (citation coverage incomplete — "
            "'uncited' below may be cited inside these):"
        )
        for name in report.missing_includes:
            lines.append(f"    {name}")
        lines.append("")

    if report.uncited:
        caveat = " — may be cited in missing includes" if report.missing_includes else ""
        lines.append(f"UNCITED (in .bib, never \\cite/\\nocite — info{caveat}):")
        lines.append("    " + ", ".join(report.uncited))
        lines.append("")

    if report.commented_twins:
        lines.append("COMMENTED ENTRIES (informational — possible preprint/version twins):")
        for k in report.commented_twins:
            lines.append(f"    {k}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
