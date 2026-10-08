r"""Run reference-audit on the HALLMARK citation-hallucination benchmark and score it.

HALLMARK (https://github.com/rpatrik96/hallmark) ships each split as JSONL: one standalone BibTeX entry
per line, keyed by a hex hash. Two commands:

* ``audit`` reads the *blind* split, so labels never reach the tool. It writes the records to ``.bib``
  chunks, audits each chunk with :func:`reference_audit.pipeline.run_audit` against one shared cache,
  and keeps one compact record per entry in ``audits.jsonl``.
* ``score`` maps every compact record to a HALLMARK prediction under two fixed mappings, runs
  HALLMARK's own ``hallmark evaluate`` on each, and writes ``summary.md``.

There are two mappings because the two tools ask different questions. reference-audit's verdict
answers *does a real document correspond to this entry?* HALLMARK's ``HALLUCINATED`` label also covers
real papers cited with wrong metadata (wrong venue, swapped or partial authors, preprint cited as
published, ...), which reference-audit reports as field findings on an ``exactly_one`` match rather
than through the verdict.

* ``identity`` scores the verdict alone: ``none`` is HALLUCINATED and ``exactly_one`` is VALID.
* ``strict`` also treats a confirmed metadata error on an ``exactly_one`` match (an ``error`` field
  finding, or a cited author absent from the matched record) as HALLUCINATED, and a field no source
  could confirm as UNCERTAIN.

``multiple`` and unresolved verdicts are UNCERTAIN under both mappings. An entry the tool did not
actually audit (the ``.bib`` round-trip changed it, or its audit raised) is UNCERTAIN with
``evaluated=false``. That marks a reported non-measurement, never a guessed label. Confidences come from
a fixed table and are not fitted to the labels.

HALLMARK pins ``bibtexparser>=2`` and this project pins ``<2``, so HALLMARK runs from its own
environment (``--hallmark-bin``) and is never imported here. ``.env`` is read from the repository root,
so the harness can be run from any directory.
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import bibtexparser
import typer
from anyascii import anyascii
from bibtexparser.bibdatabase import BibDatabase
from bibtexparser.bwriter import BibTexWriter
from bibtexparser.customization import convert_to_unicode
from pydantic import BaseModel, Field

from reference_audit.config import AuditConfig
from reference_audit.matching.names import mismatched_authors
from reference_audit.models import EntryAudit, FieldFinding
from reference_audit.parsing.bib import parse_bib
from reference_audit.pipeline import run_audit

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HALLMARK_DIR = REPO_ROOT / "benchmarks" / ".hallmark"
DEFAULT_RUNS_DIR = REPO_ROOT / "benchmarks" / "runs" / "hallmark"
DEFAULT_CACHE = DEFAULT_RUNS_DIR / ".cache" / "cache.db"
HALLMARK_VERSION = "v1.2"

# The pipeline's own marker for an entry whose audit raised (`AuditPipeline._audit_entry`).
AUDIT_FAILED_PREFIX = "audit failed"

Mapping = Literal["identity", "strict"]
MAPPINGS: tuple[Mapping, ...] = ("identity", "strict")

# P(predicted label is correct), HALLMARK's confidence convention. A fixed table, never fitted.
CONF_BY_VERDICT_CONFIDENCE = {"high": 0.9, "medium": 0.75, "low": 0.6}
CONF_FLAGGED_MATCH = 0.6     # exactly_one, but with metadata flags the mapping does not act on
CONF_METADATA_ERROR = 0.7    # strict: exactly_one with a confirmed metadata error -> HALLUCINATED
CONF_UNCERTAIN = 0.5

app = typer.Typer(
    add_completion=False,
    help="Run reference-audit on the HALLMARK benchmark and score it with HALLMARK's evaluator.",
)


class HallmarkRecord(BaseModel):
    """One line of a blind HALLMARK split."""

    bibtex_key: str
    bibtex_type: str
    fields: dict[str, str]


class CompactAudit(BaseModel):
    """The part of one entry's audit the scorer needs: a few KB instead of the full report."""

    key: str
    status: Literal["audited", "unparsed", "failed"]
    failure: str = ""                      # why status != "audited"
    verdict: Literal["none", "exactly_one", "multiple"] | None = None  # None = unresolved
    confidence: Literal["high", "medium", "low"] | None = None
    rationale: str = ""
    matched_source: str = ""
    matched_title: str = ""
    matched_authors: list[str] = Field(default_factory=list)
    matched_year: int | None = None
    matched_venue: str = ""
    matched_doi: str = ""
    author_mismatches: list[str] = Field(default_factory=list)
    findings: list[FieldFinding] = Field(default_factory=list)  # error / uncertain / unverifiable
    issues: list[str] = Field(default_factory=list)
    unresolved_reasons: list[str] = Field(default_factory=list)  # why the verdict is None
    llm_adjudications: int = 0
    from_cache: bool = False
    retried: bool = False


class Prediction(BaseModel):
    """The `hallmark.dataset.schema.Prediction` fields this harness fills."""

    bibtex_key: str
    label: Literal["VALID", "HALLUCINATED", "UNCERTAIN"]
    confidence: float
    reason: str
    evaluated: bool = True


# --------------------------------------------------------------------------------------------------
# HALLMARK records -> .bib
# --------------------------------------------------------------------------------------------------


def load_records(path: Path) -> list[HallmarkRecord]:
    records = [
        HallmarkRecord.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    dupes = [k for k, n in Counter(r.bibtex_key for r in records).items() if n > 1]
    if dupes:
        raise ValueError(f"{path}: duplicate bibtex_key(s) {dupes[:5]}; keys must be unique")
    return records


def write_bib(records: list[HallmarkRecord], path: Path) -> None:
    db = BibDatabase()
    db.entries = [
        {"ID": r.bibtex_key, "ENTRYTYPE": r.bibtex_type, **r.fields} for r in records
    ]
    writer = BibTexWriter()
    writer.order_entries_by = None  # keep split order
    writer.indent = "  "
    path.write_text(bibtexparser.dumps(db, writer), encoding="utf-8")


def write_stub_tex(bib_path: Path) -> Path:
    r"""A manuscript that cites every entry (``\nocite{*}``), the way the CLI is normally fed."""
    tex = bib_path.with_suffix(".tex")
    tex.write_text(
        "\\documentclass{article}\n\\begin{document}\n\\nocite{*}\n"
        f"\\bibliography{{{bib_path.stem}}}\n\\end{{document}}\n",
        encoding="utf-8",
    )
    return tex


def _fold(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", anyascii(value).lower())


def round_trip(
    records: list[HallmarkRecord], workdir: Path
) -> tuple[list[HallmarkRecord], dict[str, str]]:
    """Split records into those a ``.bib`` write -> `parse_bib` read leaves intact, and the rest.

    A field survives when what `parse_bib` read back equals what bibtexparser's own LaTeX decode makes
    of the original value. Anything else (an unbalanced brace that swallows the next field, a key that
    does not parse) would make reference-audit audit a different entry than HALLMARK labelled. Such a
    record is returned with its reason instead of being audited, so it is never silently dropped.
    """
    bib = workdir / "preflight.bib"
    write_bib(records, bib)
    entries, twins, unparsed = parse_bib(bib)
    parsed: dict[str, list] = defaultdict(list)
    for e in [*entries, *twins]:
        parsed[e.key].append(e)
    unparsed_why = {u.key: u.reason for u in unparsed}

    intact: list[HallmarkRecord] = []
    broken: dict[str, str] = {}
    for r in records:
        got = parsed.get(r.bibtex_key, [])
        if len(got) != 1:
            unbalanced = [
                f"{k}={v!r}" for k, v in r.fields.items() if v.count("{") != v.count("}")
            ]
            cause = f"; unbalanced braces in {', '.join(unbalanced)}" if unbalanced else ""
            if not got and r.bibtex_key in unparsed_why:
                broken[r.bibtex_key] = (
                    f".bib entry could not be parsed: {unparsed_why[r.bibtex_key]}{cause}"
                )
            else:
                broken[r.bibtex_key] = (
                    f".bib round-trip: entry parsed back {len(got)} times (expected 1){cause}"
                )
            continue
        entry = got[0]
        if entry.is_commented:
            broken[r.bibtex_key] = ".bib round-trip: entry parsed back as commented-out"
            continue
        expected = convert_to_unicode({"ID": r.bibtex_key, **r.fields})
        changed = [
            k for k in r.fields
            if _fold(entry.raw_fields.get(k.lower(), "")) != _fold(expected[k])
        ]
        if changed:
            broken[r.bibtex_key] = f".bib round-trip changed field(s): {', '.join(changed)}"
            continue
        intact.append(r)
    return intact, broken


# --------------------------------------------------------------------------------------------------
# EntryAudit -> CompactAudit
# --------------------------------------------------------------------------------------------------


def compact(audit: EntryAudit) -> CompactAudit:
    verdict = audit.verdict
    failure = next((i for i in audit.issues if i.startswith(AUDIT_FAILED_PREFIX)), "")
    best = None
    if verdict is not None and verdict.kind == "exactly_one" and verdict.artifacts:
        best = verdict.artifacts[0].best_record
    return CompactAudit(
        key=audit.entry.key,
        status="failed" if verdict is None and failure else "audited",
        failure=failure,
        verdict=verdict.kind if verdict else None,
        confidence=verdict.confidence if verdict else None,
        rationale=verdict.rationale if verdict else "",
        matched_source=best.source if best else "",
        matched_title=best.title if best else "",
        matched_authors=best.authors if best else [],
        matched_year=best.year if best else None,
        matched_venue=best.venue if best else "",
        matched_doi=(best.ids.doi or "") if best else "",
        # The same check the pipeline runs for its "author ... not found" issue (pipeline.py), taken
        # from the function rather than parsed back out of the message.
        author_mismatches=mismatched_authors(audit.entry.authors, best.authors) if best else [],
        findings=[
            f for f in audit.field_findings if f.status in ("error", "uncertain", "unverifiable")
        ],
        issues=audit.issues,
        unresolved_reasons=audit.unresolved_reasons,
        llm_adjudications=sum(1 for c in audit.candidates if c.llm is not None),
        from_cache=audit.from_cache,
    )


def not_audited(key: str, status: Literal["unparsed", "failed"], why: str) -> CompactAudit:
    return CompactAudit(key=key, status=status, failure=why)


# --------------------------------------------------------------------------------------------------
# CompactAudit -> HALLMARK prediction
# --------------------------------------------------------------------------------------------------


def _finding_note(f: FieldFinding) -> str:
    detail = f": {f.detail}" if f.detail else ""
    return f"{f.field} {f.status} ('{f.bib_value}' vs '{f.canonical_value}'){detail}"


def predict(audit: CompactAudit, mapping: Mapping) -> Prediction:
    """Map one audit to a HALLMARK prediction (see the module docstring and README for the table)."""

    def out(label: str, confidence: float, reason: str, *, evaluated: bool = True) -> Prediction:
        return Prediction(
            bibtex_key=audit.key, label=label, confidence=confidence, reason=reason,
            evaluated=evaluated,
        )

    if audit.status != "audited":
        return out(
            "UNCERTAIN", CONF_UNCERTAIN, f"not audited ({audit.status}): {audit.failure}",
            evaluated=False,
        )
    if audit.verdict is None:
        why = "; ".join(audit.unresolved_reasons or audit.issues) or "no reason recorded"
        return out("UNCERTAIN", CONF_UNCERTAIN, f"unresolved: {why}")
    if audit.verdict == "multiple":
        return out("UNCERTAIN", CONF_UNCERTAIN, f"multiple matches: {audit.rationale}")
    if audit.verdict == "none":
        conf = CONF_BY_VERDICT_CONFIDENCE[audit.confidence or "low"]
        return out("HALLUCINATED", conf, f"no match: {audit.rationale}")

    matched = f"matched {audit.matched_source} '{audit.matched_title}'"
    errors = [_finding_note(f) for f in audit.findings if f.status == "error"] + [
        f"author '{a}' not in the matched record" for a in audit.author_mismatches
    ]
    unverifiable = [_finding_note(f) for f in audit.findings if f.status == "unverifiable"]
    uncertain = [_finding_note(f) for f in audit.findings if f.status == "uncertain"]
    if errors:
        if mapping == "strict":
            return out(
                "HALLUCINATED", CONF_METADATA_ERROR,
                f"metadata error on the matched work: {'; '.join(errors)} ({matched})",
            )
        return out("VALID", CONF_FLAGGED_MATCH, f"{matched}; flags: {'; '.join(errors)}")
    if unverifiable:
        if mapping == "strict":
            return out(
                "UNCERTAIN", CONF_UNCERTAIN,
                f"{matched}; could not confirm: {'; '.join(unverifiable)}",
            )
        return out("VALID", CONF_FLAGGED_MATCH, f"{matched}; unconfirmed: {'; '.join(unverifiable)}")
    if uncertain:
        return out("VALID", CONF_FLAGGED_MATCH, f"{matched}; uncertain: {'; '.join(uncertain)}")
    conf = CONF_BY_VERDICT_CONFIDENCE[audit.confidence or "low"]
    return out("VALID", conf, matched)


# --------------------------------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------------------------------


def _git_sha(path: Path) -> str:
    try:
        sha = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"unknown ({exc})"
    return f"{sha}{'-dirty' if dirty else ''}"


def _read_jsonl(path: Path, model: type[BaseModel]) -> list:
    return [
        model.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_jsonl(path: Path, rows: list[BaseModel]) -> None:
    """Write atomically, so an interrupted run never leaves a half-written chunk that looks done."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(r.model_dump_json() + "\n" for r in rows), encoding="utf-8")
    tmp.replace(path)


def audit_chunk(
    records: list[HallmarkRecord], bib_path: Path, config: AuditConfig, cache: Path
) -> list[CompactAudit]:
    """Audit one chunk. Each entry stands alone: one failing never costs the others their result."""
    write_bib(records, bib_path)
    tex = write_stub_tex(bib_path)
    try:
        report = run_audit(tex, bib_path, config=config, cache_path=cache)
    except Exception as exc:  # the whole chunk is lost: report every entry, never drop them
        why = f"chunk audit raised: {type(exc).__name__}: {exc}"
        return [not_audited(r.bibtex_key, "failed", why) for r in records]
    by_key = {a.entry.key: a for a in report.entries}
    unparsed = {u.key: u.reason for u in report.unparsed}

    def one(key: str) -> CompactAudit:
        if key in by_key:
            return compact(by_key[key])
        if key in unparsed:
            return not_audited(key, "unparsed", f".bib entry could not be parsed: {unparsed[key]}")
        return not_audited(key, "failed", "entry missing from the audit report")

    return [one(r.bibtex_key) for r in records]


def _log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def _run_chunks(
    records: list[HallmarkRecord], prefix: str, workdir: Path, chunk_size: int,
    config: AuditConfig, cache: Path,
) -> dict[str, CompactAudit]:
    results: dict[str, CompactAudit] = {}
    chunks = [records[i:i + chunk_size] for i in range(0, len(records), chunk_size)]
    for n, chunk in enumerate(chunks):
        done = workdir / f"{prefix}_{n:03d}.jsonl"
        keys = [r.bibtex_key for r in chunk]
        if done.exists():
            rows = _read_jsonl(done, CompactAudit)
            if [r.key for r in rows] == keys:
                results.update({r.key: r for r in rows})
                _log(f"{prefix} {n + 1}/{len(chunks)}: already done, reusing {done.name}")
                continue
            _log(f"{prefix} {n + 1}/{len(chunks)}: {done.name} does not match this chunk; redoing")
        started = time.monotonic()
        rows = audit_chunk(chunk, workdir / f"{prefix}_{n:03d}.bib", config, cache)
        _write_jsonl(done, rows)
        results.update({r.key: r for r in rows})
        counts = Counter(r.verdict or ("unresolved" if r.status == "audited" else r.status)
                         for r in rows)
        _log(
            f"{prefix} {n + 1}/{len(chunks)}: {len(rows)} entries in "
            f"{time.monotonic() - started:.0f}s {dict(counts)}"
        )
    return results


def _counts(audits: list[CompactAudit]) -> dict[str, int]:
    c = Counter(
        a.status if a.status != "audited" else (a.verdict or "unresolved") for a in audits
    )
    return dict(sorted(c.items()))


@app.command()
def audit(
    split: str = typer.Option("dev_public", help="HALLMARK split (its *_blind.jsonl is audited)."),
    hallmark_dir: Path = typer.Option(
        DEFAULT_HALLMARK_DIR, exists=True, file_okay=False, help="HALLMARK checkout."
    ),
    out: Path | None = typer.Option(
        None, help="Run directory (default: benchmarks/runs/hallmark/<split>)."
    ),
    cache: Path = typer.Option(DEFAULT_CACHE, help="reference-audit cache DB, shared across runs."),
    limit: int = typer.Option(0, help="Audit a random sample of this many entries (0 = all)."),
    seed: int = typer.Option(0, help="Sampling seed for --limit."),
    chunk_size: int = typer.Option(200, min=1, help="Entries per audit chunk (the resume unit)."),
    retry_unresolved: int = typer.Option(
        1, min=0,
        help="Re-audit unresolved entries this many times (errors are never cached, so only the "
             "failed queries repeat).",
    ),
    no_llm: bool = typer.Option(False, "--no-llm", help="Run without LLM adjudication."),
    backend: str | None = typer.Option(
        None, help="Source backend for Semantic Scholar/OpenAlex/DBLP: api | clickhouse "
                   "(default: SOURCE_BACKEND in .env, else api)."
    ),
) -> None:
    """Audit a blind HALLMARK split with reference-audit; resumable, one compact record per entry."""
    out = out or DEFAULT_RUNS_DIR / split
    out.mkdir(parents=True, exist_ok=True)
    cache.parent.mkdir(parents=True, exist_ok=True)
    workdir = out / "chunks"
    workdir.mkdir(exist_ok=True)

    config = AuditConfig(_env_file=REPO_ROOT / ".env")
    if backend is not None:
        if backend not in ("api", "clickhouse"):
            raise typer.BadParameter("--backend must be 'api' or 'clickhouse'")
        config = config.model_copy(update={"source_backend": backend})
    if no_llm:
        config = config.model_copy(update={"use_llm": False})
    elif not config.llm_enabled():
        # The pipeline silently runs formal-only without a key; a benchmark run must not.
        raise typer.BadParameter(
            f"no OPENAI_API_KEY in the environment or {REPO_ROOT / '.env'}: the LLM would be "
            "silently disabled. Set it, or pass --no-llm to measure the formal-only pipeline."
        )

    run_meta_path = out / "run.json"
    identity = {
        "split": split,
        "limit": limit,
        "seed": seed,
        "pipeline_version": config.pipeline_version,
        "model": config.model,
        "llm_enabled": config.llm_enabled(),
        "source_backend": config.source_backend,
    }
    previous: dict = {}
    if run_meta_path.exists():
        previous = json.loads(run_meta_path.read_text(encoding="utf-8"))
        clash = {k: (previous.get(k), v) for k, v in identity.items() if previous.get(k) != v}
        if clash:
            raise typer.BadParameter(
                f"{out} holds a run with different settings {clash}; resume it with the same "
                "settings or pass a new --out."
            )

    records = load_records(hallmark_dir / "data" / HALLMARK_VERSION / f"{split}_blind.jsonl")
    if limit and limit < len(records):
        chosen = set(random.Random(seed).sample([r.bibtex_key for r in records], limit))
        records = [r for r in records if r.bibtex_key in chosen]

    started_at = datetime.now(UTC)
    started = time.monotonic()
    intact, broken = round_trip(records, workdir)
    _log(f"{split}: {len(records)} entries, {len(broken)} fail the .bib round-trip")
    for key, why in broken.items():
        _log(f"  unparsed {key}: {why}")

    results = _run_chunks(intact, "chunk", workdir, chunk_size, config, cache)
    counts_before_retry = _counts(list(results.values()))
    for attempt in range(1, retry_unresolved + 1):
        # Unresolved and failed entries alike: neither outcome is cached, so a rerun is a real retry.
        pending = [r for r in intact if results[r.bibtex_key].verdict is None]
        if not pending:
            break
        _log(f"retry {attempt}: re-auditing {len(pending)} unresolved/failed entries")
        retried = _run_chunks(pending, f"retry{attempt}", workdir, chunk_size, config, cache)
        for key, a in retried.items():
            results[key] = a.model_copy(update={"retried": True})

    audits = [
        results[r.bibtex_key] if r.bibtex_key in results
        else not_audited(r.bibtex_key, "unparsed", broken[r.bibtex_key])
        for r in records
    ]
    _write_jsonl(out / "audits.jsonl", audits)
    meta = {
        **identity,
        "hallmark_dir": str(hallmark_dir),
        "hallmark_version": HALLMARK_VERSION,
        "hallmark_sha": _git_sha(hallmark_dir),
        "reference_audit_sha": _git_sha(REPO_ROOT),
        "cache": str(cache),
        "chunk_size": chunk_size,
        "retry_unresolved": retry_unresolved,
        # A resumed run keeps its first start and adds this invocation's time to the earlier ones.
        "started_at": previous.get("started_at", started_at.isoformat()),
        "wall_seconds": previous.get("wall_seconds", 0) + round(time.monotonic() - started),
        "entries": len(audits),
        "counts": _counts(audits),
        "counts_before_retry": counts_before_retry,
        "from_cache": sum(a.from_cache for a in audits),
        "llm_adjudications": sum(a.llm_adjudications for a in audits),
    }
    run_meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    _log(f"done: {meta['counts']} -> {out / 'audits.jsonl'}")


# --------------------------------------------------------------------------------------------------
# score
# --------------------------------------------------------------------------------------------------

_METRICS = [
    ("detection_rate", "DR"),
    ("false_positive_rate", "FPR"),
    ("f1_hallucination", "F1"),
    ("tier_weighted_f1", "TW-F1"),
    ("tier3_f1", "Tier-3 F1"),
    ("mcc", "MCC"),
    ("auroc", "AUROC"),
    ("ece", "ECE"),
    ("num_uncertain", "UNCERTAIN"),
    ("num_evaluated", "evaluated"),
    ("coverage", "coverage"),
]


def _fmt(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _short(text: str, n: int = 240) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _relabel_note(e: dict) -> str:
    """HALLMARK's own label history, which marks a contested label next to a disagreement."""
    if "relabeled_from" not in e:
        return ""
    return (
        f" [HALLMARK relabelled {e['relabeled_from']} → {e['label']}: "
        f"{_short(e.get('relabel_reason') or '', 160)}]"
    )


def build_summary(
    split: str,
    meta: dict,
    labeled: list[dict],
    predictions: dict[Mapping, dict[str, Prediction]],
    results: dict[Mapping, dict],
) -> str:
    lines = [
        f"# reference-audit on HALLMARK `{split}`",
        "",
        f"- entries: {meta['entries']} — audit outcomes {meta['counts']}",
        f"- audit outcomes before retrying unresolved/failed entries: {meta['counts_before_retry']}",
        f"- reference-audit `{meta['reference_audit_sha']}`, pipeline {meta['pipeline_version']}, "
        f"model `{meta['model']}` (LLM {'on' if meta['llm_enabled'] else 'off'}), "
        f"source backend `{meta.get('source_backend', 'api')}`",
        f"- HALLMARK `{meta['hallmark_sha']}` ({meta['hallmark_version']})",
        f"- audit wall time: {meta['wall_seconds'] / 60:.0f} min (cache hits: {meta['from_cache']})",
        "",
        "Conservative mode leaves UNCERTAIN out of the classification metrics; aggressive mode "
        "counts it as HALLUCINATED. `evaluated=false` predictions are excluded in both.",
        "",
        "## Metrics",
        "",
        "| mapping | mode | " + " | ".join(name for _, name in _METRICS) + " |",
        "|---|---|" + "---|" * len(_METRICS),
    ]
    for mapping in MAPPINGS:
        for mode in ("conservative", "aggressive"):
            r = results[mapping][mode]
            row = " | ".join(_fmt(r.get(field)) for field, _ in _METRICS)
            lines.append(f"| {mapping} | {mode} | {row} |")

    # A VALID row may omit `hallucination_type` / `difficulty_tier` altogether: in v1.2 the entries
    # relabelled HALLUCINATED -> VALID carry neither key.
    by_type: dict[str, list[dict]] = defaultdict(list)
    for e in labeled:
        by_type[e.get("hallucination_type") or "VALID"].append(e)
    lines += [
        "",
        "## Per type",
        "",
        "Counts of predicted labels (H = HALLUCINATED, V = VALID, U = UNCERTAIN, "
        "x = not evaluated). For VALID entries, H is a false positive.",
        "",
        "| type | tier | n | identity H / V / U / x | strict H / V / U / x |",
        "|---|---|---|---|---|",
    ]

    def tally(mapping: Mapping, entries: list[dict]) -> str:
        c: Counter = Counter()
        for e in entries:
            p = predictions[mapping][e["bibtex_key"]]
            c["x" if not p.evaluated else p.label[0]] += 1
        return " / ".join(str(c[k]) for k in ("H", "V", "U", "x"))

    order = sorted(
        by_type, key=lambda t: (t != "VALID", by_type[t][0].get("difficulty_tier") or 0, t)
    )
    for t in order:
        es = by_type[t]
        tier = es[0].get("difficulty_tier") or "—"
        lines.append(
            f"| {t} | {tier} | {len(es)} | {tally('identity', es)} | {tally('strict', es)} |"
        )

    for mapping in MAPPINGS:
        fps = [e for e in labeled if e["label"] == "VALID"
               and predictions[mapping][e["bibtex_key"]].label == "HALLUCINATED"]
        lines += ["", f"## False positives — `{mapping}` ({len(fps)})", ""]
        for e in fps:
            p = predictions[mapping][e["bibtex_key"]]
            lines.append(
                f"- `{e['bibtex_key']}` {_short(e['fields'].get('title', ''), 100)} "
                f"({e['fields'].get('year', '?')}) — {_short(p.reason)}{_relabel_note(e)}"
            )

    unresolved = [e for e in labeled
                  if predictions["identity"][e["bibtex_key"]].reason.startswith("unresolved:")]
    lines += ["", f"## Unresolved ({len(unresolved)})", ""]
    for e in unresolved:
        p = predictions["identity"][e["bibtex_key"]]
        lines.append(f"- `{e['bibtex_key']}` ({e['label']}) — {_short(p.reason)}")

    lines += ["", "## Misses — `strict` (hallucinated, predicted VALID)", ""]
    for t in order:
        if t == "VALID":
            continue
        missed = [e for e in by_type[t] if predictions["strict"][e["bibtex_key"]].label == "VALID"]
        if not missed:
            continue
        lines += [f"### {t} ({len(missed)} of {len(by_type[t])})", ""]
        for e in missed:
            p = predictions["strict"][e["bibtex_key"]]
            lines.append(
                f"- `{e['bibtex_key']}` {_short(e['fields'].get('title', ''), 100)} — "
                f"{_short(p.reason)}{_relabel_note(e)}"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


@app.command()
def score(
    split: str = typer.Option("dev_public", help="HALLMARK split the run audited."),
    hallmark_dir: Path = typer.Option(
        DEFAULT_HALLMARK_DIR, exists=True, file_okay=False, help="HALLMARK checkout."
    ),
    hallmark_bin: Path | None = typer.Option(
        None, help="HALLMARK's CLI in its own env (default: <hallmark-dir>/.venv/bin/hallmark)."
    ),
    out: Path | None = typer.Option(
        None, help="Run directory (default: benchmarks/runs/hallmark/<split>)."
    ),
) -> None:
    """Map audits to predictions (identity + strict), run `hallmark evaluate`, write summary.md."""
    out = out or DEFAULT_RUNS_DIR / split
    hallmark_bin = hallmark_bin or hallmark_dir / ".venv" / "bin" / "hallmark"
    meta = json.loads((out / "run.json").read_text(encoding="utf-8"))
    if meta["split"] != split:
        raise typer.BadParameter(f"{out} audited split {meta['split']!r}, not {split!r}")
    audits: list[CompactAudit] = _read_jsonl(out / "audits.jsonl", CompactAudit)

    # Evaluate on exactly the audited entries, so a --limit run is scored on its sample. Every one of
    # them gets a prediction: the key sets are equal (audit keys are unique) or scoring stops here.
    # HALLMARK's own `--strict` is not used for this, since it counts an `evaluated=false` prediction
    # as missing and a not-audited entry must stay a reported non-measurement.
    labeled_all = [
        json.loads(line)
        for line in (hallmark_dir / "data" / HALLMARK_VERSION / f"{split}.jsonl")
        .read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    audited_keys = {a.key for a in audits}
    labeled = [e for e in labeled_all if e["bibtex_key"] in audited_keys]
    if len(labeled) != len(audits):
        raise typer.BadParameter(
            f"{len(audits) - len(labeled)} audited keys are not in HALLMARK's labeled {split}"
        )
    data_dir = out / "hallmark_data"
    (data_dir / HALLMARK_VERSION).mkdir(parents=True, exist_ok=True)
    (data_dir / HALLMARK_VERSION / f"{split}.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in labeled), encoding="utf-8"
    )

    predictions: dict[Mapping, dict[str, Prediction]] = {}
    results: dict[Mapping, dict] = {}
    for mapping in MAPPINGS:
        preds = [predict(a, mapping) for a in audits]
        predictions[mapping] = {p.bibtex_key: p for p in preds}
        pred_path = out / f"predictions.{mapping}.jsonl"
        _write_jsonl(pred_path, preds)
        result_path = out / f"eval.{mapping}.json"
        cmd = [
            str(hallmark_bin), "evaluate",
            "--split", split,
            "--version", HALLMARK_VERSION,
            "--data-dir", str(data_dir),
            "--predictions", str(pred_path),
            "--tool-name", f"reference-audit-{mapping}",
            "--eval-mode", "both",
            "--detailed",
            "--output", str(result_path),
        ]
        _log(f"{mapping}: {' '.join(cmd)}")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        (out / f"eval.{mapping}.txt").write_text(proc.stdout + proc.stderr, encoding="utf-8")
        if proc.returncode != 0:
            raise RuntimeError(
                f"hallmark evaluate failed for {mapping} (exit {proc.returncode}); see "
                f"{out / f'eval.{mapping}.txt'}:\n{proc.stderr[-2000:]}"
            )
        results[mapping] = json.loads(result_path.read_text(encoding="utf-8"))

    summary = build_summary(split, meta, labeled, predictions, results)
    (out / "summary.md").write_text(summary, encoding="utf-8")
    _log(f"wrote {out / 'summary.md'}")


if __name__ == "__main__":
    app()
