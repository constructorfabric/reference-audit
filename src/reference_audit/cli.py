"""`reference-audit` command-line interface (Typer).

`audit` runs the async pipeline (parse + identify, cached) over either a `.tex` + `.bib` pair or a
single PDF. `--no-network` gives the parse-only report (pair input only — a PDF's reference list comes
from an HTTP call to GROBID, so there is no offline parse for it).
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from reference_audit.config import AuditConfig
from reference_audit.inputs import InvalidInputError, PdfInput, default_cache_path, resolve_input
from reference_audit.pdf.grobid import GrobidError
from reference_audit.pipeline import (
    EmptyBibliographyError,
    build_parse_report,
    run_audit,
    run_pdf_audit,
)
from reference_audit.report import render_json, render_text
from reference_audit.sources.clickhouse import ClickHouseUnavailableError

app = typer.Typer(
    add_completion=False,
    help="Audit .bib/.tex or PDF references: identify artifacts, screen for hallucinations.",
)


@app.callback()
def main() -> None:
    """Reference auditor — identify exact artifacts and screen for hallucinated references."""


@app.command()
def audit(
    document: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        help="Manuscript .tex (paired with a .bib), or a .pdf whose references GROBID extracts.",
    ),
    bib: Path | None = typer.Argument(
        None,
        exists=True,
        dir_okay=False,
        help="Bibliography .bib to audit. Required with a .tex; must be omitted with a .pdf.",
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
    backend: str | None = typer.Option(
        None, "--backend",
        help="Where Semantic Scholar, OpenAlex and DBLP are read from: api | clickhouse "
             "(default: SOURCE_BACKEND, else api).",
    ),
    grobid: str | None = typer.Option(
        None, "--grobid", help="GROBID base URL for PDF input (default http://localhost:8070)."
    ),
    fail_on: str | None = typer.Option(
        None, "--fail-on", help="Exit non-zero if any verdict matches: hallucinated | multiple."
    ),
    partial_authors: str | None = typer.Option(
        None, "--partial-authors",
        help="A cited author list that omits some of the work's authors without 'and others': "
             "ignore | warn | error (default: PARTIAL_AUTHORS, else warn).",
    ),
) -> None:
    """Audit a .bib with its .tex, or a PDF on its own.

    With a PDF, GROBID supplies both the reference list and the in-text citations, so no .bib is
    needed. Either way each reference is identified and screened for hallucinations.
    """
    if fmt not in ("text", "json", "both"):
        raise typer.BadParameter("format must be one of: text, json, both")
    if backend not in (None, "api", "clickhouse"):
        raise typer.BadParameter("--backend must be 'api' or 'clickhouse'")
    if partial_authors not in (None, "ignore", "warn", "error"):
        raise typer.BadParameter("--partial-authors must be 'ignore', 'warn' or 'error'")

    try:
        source = resolve_input(document, bib)
    except InvalidInputError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if no_network and isinstance(source, PdfInput):
        # --no-network is a contract ("this run contacts nothing"), and --grobid can point at any
        # host, so we cannot honestly weaken it to "no *remote* network" for a PDF. Reject it rather
        # than quietly redefining what the flag promises.
        raise typer.BadParameter(
            "--no-network cannot be combined with a PDF: the reference list and the in-text "
            "citations are extracted by the GROBID service over HTTP, so a PDF has no offline parse "
            "path. Pass a .tex + .bib pair for the offline parse-only report."
        )

    try:
        if no_network:
            report = build_parse_report(source.tex_path, source.bib_path)
        else:
            updates: dict = {}
            if model:
                updates["model"] = model
            if no_llm:
                updates["use_llm"] = False
            if check_citations:
                updates["check_alignment"] = True
            if grobid:
                updates["grobid_url"] = grobid
            if backend:
                updates["source_backend"] = backend
            if partial_authors:
                updates["partial_authors"] = partial_authors
            config = AuditConfig().model_copy(update=updates)
            cache_path = cache or default_cache_path(source)
            common = {
                "config": config,
                "cache_path": cache_path,
                "fresh": fresh,
                "progress": sys.stderr.isatty(),
            }
            if isinstance(source, PdfInput):
                report = run_pdf_audit(source.pdf_path, **common)
            else:
                report = run_audit(source.tex_path, source.bib_path, **common)
    except (EmptyBibliographyError, GrobidError, ClickHouseUnavailableError) as exc:
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
