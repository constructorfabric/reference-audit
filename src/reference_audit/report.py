"""Render an AuditReport as JSON (machine, use-case c) or human-readable text."""

from __future__ import annotations

from reference_audit.models import AuditReport, EntryAudit, FieldFinding


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
    vline = _verdict_line(a)
    if vline:
        block.append(vline)
    return block


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
    by_type = s.get("by_type", {})
    if by_type:
        lines.append("  types: " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))
    lines.append("")

    # Group entries so the reader sees the actionable ones first: issues, then entries whose only
    # finding is a cosmetic formatting nit, then the clean ones. (`network` gates the verdict-aware
    # half of the issue test — in --no-network mode there are no verdicts to read.)
    network = verdicts is not None
    with_issues: list[EntryAudit] = []
    nits_only: list[EntryAudit] = []
    clean: list[EntryAudit] = []
    for a in report.entries:
        if _has_issues(a, network=network):
            with_issues.append(a)
        elif _formatting_nits(a):
            nits_only.append(a)
        else:
            clean.append(a)

    groups = [
        (with_issues, "ISSUES ({n}) — problems, possible hallucinations, or could-not-verify:"),
        (nits_only, "FORMATTING NITS ({n}) — verified; only cosmetic field fixes:"),
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
