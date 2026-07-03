"""`reference-audit` command-line interface (Typer).

`audit` runs the async pipeline (parse + identify, cached). `--no-network` gives the M1 parse-only
report. `--no-llm`/`--fail-on` are accepted now and become load-bearing as later milestones land.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from reference_audit.config import AuditConfig
from reference_audit.pipeline import EmptyBibliographyError, build_parse_report, run_audit
from reference_audit.report import render_json, render_text

app = typer.Typer(
    add_completion=False,
    help="Audit .bib/.tex references: identify artifacts, screen for hallucinations.",
)


@app.callback()
def main() -> None:
    """Reference auditor — identify exact artifacts and screen for hallucinated references."""


@app.command()
def audit(
    tex: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Manuscript .tex (for cited/uncited bookkeeping)."
    ),
    bib: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Bibliography .bib to audit."
    ),
    fmt: str = typer.Option("text", "--format", "-f", help="Output format: text | json | both."),
    no_network: bool = typer.Option(
        False, "--no-network", help="Parse-only: identifiers + cited/uncited, no DB calls."
    ),
    no_llm: bool = typer.Option(
        False, "--no-llm", help="Formal-only: skip LLM adjudication (deterministic)."
    ),
    check_citations: bool = typer.Option(
        False, "--check-citations",
        help="Advisory: check each citing context against the cited work's abstract (needs the LLM).",
    ),
    fresh: bool = typer.Option(False, "--fresh", help="Ignore cached results and re-query."),
    cache: Path | None = typer.Option(
        None, "--cache", help="Cache DB path (default: <bib_dir>/.reference_audit/cache.db)."
    ),
    model: str | None = typer.Option(None, "--model", help="LLM model override."),
    fail_on: str | None = typer.Option(
        None, "--fail-on", help="Exit non-zero if any verdict matches: hallucinated | multiple."
    ),
) -> None:
    """Audit a .bib with its .tex. Identifies each reference and screens for hallucinations."""
    if fmt not in ("text", "json", "both"):
        raise typer.BadParameter("format must be one of: text, json, both")

    try:
        if no_network:
            report = build_parse_report(tex, bib)
        else:
            updates: dict = {}
            if model:
                updates["model"] = model
            if no_llm:
                updates["use_llm"] = False
            if check_citations:
                updates["check_alignment"] = True
            config = AuditConfig().model_copy(update=updates)
            cache_path = cache or (bib.parent / ".reference_audit" / "cache.db")
            report = run_audit(
                tex,
                bib,
                config=config,
                cache_path=cache_path,
                fresh=fresh,
                progress=sys.stderr.isatty(),
            )
    except EmptyBibliographyError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if fmt in ("json", "both"):
        typer.echo(render_json(report))
    if fmt in ("text", "both"):
        typer.echo(render_text(report))

    if fail_on:
        verdicts = report.summary.get("verdicts", {})
        trigger = {"hallucinated": "none", "multiple": "multiple"}.get(fail_on)
        if trigger is None:
            raise typer.BadParameter("--fail-on must be 'hallucinated' or 'multiple'")
        if verdicts.get(trigger, 0) > 0:
            raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
