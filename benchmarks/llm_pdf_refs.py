r"""Experiment: extract a PDF's reference list with an OpenAI model instead of GROBID, and score both.

The PDF goes to the Responses API as an ``input_file`` (the model sees the text layer and the page
images), and the answer comes back as a pydantic :class:`ReferenceList`. Each reference then goes
through :func:`reference_audit.parsing.bib.entry_from_fields`, the same construction seam ``pdf.tei``
uses, so both extractors are scored on identical ``BibEntry`` objects.

The commands, all run from the repository root:

* ``extract`` sends each PDF to each model and keeps the raw structured answer, token usage and
  latency in ``<out>/<schema>/<model>/<pdf stem>.json``. A file already there is not re-requested; a
  failed call (including a refusal or a cut-off answer) is written as ``{"error": ...}`` and retried
  on the next run. ``--schema v1`` is the plain transcription schema; ``v2`` (the default) adds
  nullable regex/range constraints on identifiers and the year, and per-entry ``doubts``.
* ``roundtrip`` scores the extractions of the compiled test documents (``tests/documents/*/.build``)
  with the same oracle as ``tests/test_pdf_extraction.py``, next to GROBID's cached TEI.
* ``ai4x`` scores extractions of third-party PDFs against hand-built ground truth
  (``<gt>/<stem>.json``, one per PDF, format in ``docs/archive/llm-pdf-references.md``) and against
  GROBID (``<grobid>/<stem>.tei.xml``, fetched with ``grobid`` beforehand).

* ``runs`` prints one line per run on each roundtrip document (every field error, minus the
  ``.bib`` oracle's artifacts), for measuring run-to-run variance. A model may be given as
  ``model:effort`` (e.g. ``gpt-6-luna:high``) to set the reasoning effort; ``extract --repeat N``
  makes the repeated runs.

The scorers also run two deterministic DOI checks a production version would need: every
extracted DOI must occur in the PDF's own text layer (``doi_guard``), and no DOI may be carried by
two entries with different titles (``shared_dois``).

Nothing here is wired into ``reference-audit``; it is a measurement harness.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Literal

import typer
from pydantic import BaseModel, Field

from reference_audit.config import AuditConfig
from reference_audit.matching.features import title_ratio
from reference_audit.matching.names import author_overlap, last_name
from reference_audit.models import BibEntry
from reference_audit.parsing.bib import entry_from_fields
from reference_audit.pdf.llm_refs import INSTRUCTIONS as PRODUCT_INSTRUCTIONS
from reference_audit.pdf.llm_refs import PrintedReferenceList, list_problems
from reference_audit.pdf.tei import parse_tei

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import pdf_oracle  # tests/ is on the path only from here

_ORACLE_TIER, _ORACLE_SCORE = pdf_oracle._tier, pdf_oracle._score

app = typer.Typer(add_completion=False, no_args_is_help=True)

DEFAULT_MODELS = ("gpt-6.1-sol", "gpt-6-luna")
DEFAULT_OUT = ROOT / "benchmarks" / "runs" / "llm_pdf_refs"


# =================================================================================================
# Structured output
# =================================================================================================


class ExtractedReference(BaseModel):
    """One entry of the printed reference list, transcribed — not looked up, not completed."""

    label: str | None = Field(description="Printed label without brackets or dot ('12'), or null.")
    raw_text: str = Field(description="The whole entry exactly as printed, joined into one line.")
    entry_type: Literal[
        "article", "inproceedings", "book", "incollection", "thesis", "report", "misc"
    ]
    title: str | None = Field(description="The printed title, or null if the style prints none.")
    authors: list[str] = Field(
        description="Printed person or organization names, in order; never 'et al.'."
    )
    authors_truncated: bool = Field(description="True if 'et al.' or an ellipsis is printed.")
    year: int | None
    venue: str | None = Field(description="Journal, proceedings or series as printed.")
    publisher: str | None
    volume: str | None
    issue: str | None
    pages: str | None
    doi: str | None = Field(description="Only a DOI printed in this entry, without doi.org prefix.")
    arxiv_id: str | None = Field(description="Only an arXiv id printed in this entry.")
    isbn: str | None
    url: str | None = Field(description="A printed URL that is not a DOI or arXiv link.")


class ReferenceList(BaseModel):
    has_reference_list: bool = Field(
        description="False when the document prints no bibliography or reference list."
    )
    references: list[ExtractedReference]
    problems: list[str] = Field(
        description="Anything that could not be read reliably, one item per problem."
    )


INSTRUCTIONS = """\
You transcribe the reference list (bibliography) of the attached PDF into structured records.

Rules:
- Transcribe only what is printed in the document's reference list. Never look anything up, never
  correct, complete or normalize a reference from your own knowledge, never add a DOI, arXiv id, year
  or author that is not printed in that entry. A field that is not printed is null.
- A reference list is a References/Bibliography/Literature section, an unheaded numbered list at the
  end, or reference footnotes. Citations that appear only inline in the body text are not part of it.
  If the document has no reference list, return has_reference_list=false and an empty list.
- Return every entry, in printed reading order (in a two-column layout: left column top to bottom,
  then right column). Do not merge two entries or split one.
- authors: one string per printed person or organization, as printed ("Smith, J." or "J. Smith").
  Do not include "et al."; set authors_truncated instead.
- doi: the bare DOI (10.xxxx/...) as printed, also when printed as a https://doi.org/ link. A DOI
  broken across lines is joined without the line-break hyphen only if the hyphen is clearly not part
  of the DOI; report the doubt in problems.
- If text is unreadable or ambiguous, transcribe what you can and describe the issue in problems.
"""

# v2: the same transcription, with the constraints structured outputs can enforce while decoding.
# Every constrained field is nullable, and the instructions say to use null rather than repair: a
# constraint guarantees the *shape* of a value, not that it was printed, and a model with no escape
# hatch emits a well-formed wrong value (a space in a DOI became "%20" when we tried).
DOI_PATTERN = r"^10\.[0-9]{4,9}/\S+$"
ARXIV_PATTERN = r"^([0-9]{4}\.[0-9]{4,5}|[a-z][a-z.\-]*/[0-9]{7})(v[0-9]+)?$"
ISBN_PATTERN = r"^[0-9][0-9\- ]{8,15}[0-9Xx]$"
URL_PATTERN = r"^https?://\S+$"


class ExtractedReferenceV2(BaseModel):
    """One entry of the printed reference list, transcribed — not looked up, not completed."""

    # Output follows schema key order (and this docstring is sent as the schema description, so the
    # rationale lives here): `raw_text` comes first so the fields are read off the transcription. A
    # `printed_author_count` placed before `authors` was tried and dropped: on long author lists it
    # disagreed with correct lists three times and agreed with a list that had lost a name.
    raw_text: str = Field(description="The whole entry exactly as printed, joined into one line.")
    label: str | None = Field(description="Printed label without brackets or dot ('12'), or null.")
    entry_type: Literal[
        "article", "inproceedings", "book", "incollection", "thesis", "report", "misc"
    ]
    title: str | None = Field(description="The printed title, or null if the style prints none.")
    authors: list[str] = Field(
        description="Printed person or organization names, in order; never 'et al.'."
    )
    authors_truncated: bool = Field(description="True if 'et al.' or an ellipsis is printed.")
    year: int | None = Field(ge=1000, le=2999)
    venue: str | None = Field(description="Journal, proceedings or series as printed.")
    publisher: str | None
    volume: str | None
    issue: str | None
    pages: str | None
    doi: str | None = Field(
        pattern=DOI_PATTERN,
        description="The bare DOI printed in this entry; null if none, or if what is printed is not "
        "a well-formed DOI.",
    )
    arxiv_id: str | None = Field(
        pattern=ARXIV_PATTERN, description="An arXiv id printed in this entry, without 'arXiv:'."
    )
    isbn: str | None = Field(pattern=ISBN_PATTERN, description="An ISBN printed in this entry.")
    url: str | None = Field(
        pattern=URL_PATTERN, description="A printed URL that is not a DOI or arXiv link."
    )
    doubts: list[str] = Field(
        description="One item per judgment that could change a value: a field left null because "
        "it is printed malformed, a line break inside an identifier or a name, an unclear entry "
        "boundary. Never ordinary word hyphenation."
    )


class ReferenceListV2(BaseModel):
    has_reference_list: bool = Field(
        description="False when the document prints no bibliography or reference list."
    )
    references: list[ExtractedReferenceV2]
    problems: list[str] = Field(
        description="Problems with the list as a whole (unreadable pages, unclear order)."
    )


INSTRUCTIONS_V2 = INSTRUCTIONS.replace(
    "  of the DOI; report the doubt in problems.\n",
    "  of the DOI; record the judgment in that entry's doubts.\n",
).replace(
    "- If text is unreadable or ambiguous, transcribe what you can and describe the issue in problems.\n",
    """\
- raw_text first, then read every other field off it.
- doi, arxiv_id, isbn, url and year have a fixed format. If what is printed does not fit it, set the
  field to null and say why in doubts. Never repair a value into a well-formed one you did not see
  printed; joining a value that a line break split is the only allowed change. If an entry prints
  the same identifier twice in different forms (a "doi:" field and a doi.org URL), use the
  well-formed one and record the difference in doubts.
- Doubts about one entry go in its doubts; problems is only for the list as a whole. Do not report
  ordinary words hyphenated at a line end; report a line break only when it falls inside an
  identifier, a URL or a name and the hyphen could belong to it.
""",
)
assert INSTRUCTIONS_V2 != INSTRUCTIONS

# v3 is the product's schema (`reference_audit.pdf.llm_refs`): v2 plus the completeness fields — the
# last printed entry named before the list, the coverage reported after it.
SCHEMAS: dict[str, tuple[type[BaseModel], str]] = {
    "v1": (ReferenceList, INSTRUCTIONS),
    "v2": (ReferenceListV2, INSTRUCTIONS_V2),
    "v3": (PrintedReferenceList, PRODUCT_INSTRUCTIONS),
}


def _pdf_input(pdf: Path) -> dict:
    data = base64.b64encode(pdf.read_bytes()).decode("ascii")
    return {"type": "input_file", "filename": pdf.name,
            "file_data": f"data:application/pdf;base64,{data}"}


def _refusal(resp) -> str | None:
    for item in resp.output or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", "") == "refusal":
                return part.refusal
    return None


def _model_spec(spec: str) -> tuple[str, dict]:
    """`gpt-6-luna:high` → ("gpt-6-luna", {"reasoning": {"effort": "high"}}); no suffix keeps the
    model's default effort. The spec, suffix included, names the output directory."""
    model, _, effort = spec.partition(":")
    return model, ({"reasoning": {"effort": effort}} if effort else {})


async def _extract_one(client, spec: str, schema: str, pdf: Path) -> dict:
    """One extraction. A refusal or a cut-off answer raises with its reason — a truncated reference
    list must never be scored (or used) as if it were the whole list."""
    text_format, instructions = SCHEMAS[schema]
    model, extra = _model_spec(spec)
    started = time.monotonic()
    resp = await client.responses.parse(
        model=model,
        **extra,
        instructions=instructions,
        input=[{"role": "user", "content": [
            _pdf_input(pdf),
            {"type": "input_text", "text": "Transcribe this document's reference list."},
        ]}],
        text_format=text_format,
    )
    if (refusal := _refusal(resp)) is not None:
        raise RuntimeError(f"the model refused: {refusal}")
    if resp.status == "incomplete":
        raise RuntimeError(f"incomplete response: {resp.incomplete_details}")
    if resp.output_parsed is None:
        raise RuntimeError(f"no parsed output (status={resp.status})")
    usage = resp.usage.model_dump() if resp.usage else {}
    return {"model": spec, "schema": schema, "pdf": pdf.name,
            "seconds": round(time.monotonic() - started, 1),
            "usage": usage, "result": resp.output_parsed.model_dump()}


@app.command()
def extract(
    pdfs: list[Path] = typer.Argument(..., help="PDF files to extract."),
    model: list[str] = typer.Option(
        list(DEFAULT_MODELS), help="OpenAI models (repeatable), optionally `model:effort`."
    ),
    schema: str = typer.Option("v2", help="Output schema: v1 (plain) or v2 (constrained)."),
    repeat: int = typer.Option(1, help="Runs per (model, PDF); run k>1 is saved as <stem>.r<k>.json."),
    out: Path = typer.Option(DEFAULT_OUT / "extractions"),
    concurrency: int = typer.Option(6),
) -> None:
    """Run each model on each PDF, caching the structured answer per (schema, model, PDF)."""
    if schema not in SCHEMAS:
        raise typer.BadParameter(f"unknown schema {schema!r}; choose from {sorted(SCHEMAS)}")
    from openai import AsyncOpenAI

    cfg = AuditConfig()
    if not cfg.openai_api_key:
        raise typer.BadParameter("no OpenAI key configured (openai_api_key)")

    async def go() -> None:
        client = AsyncOpenAI(api_key=cfg.openai_api_key, base_url=cfg.openai_base_url,
                             max_retries=4, timeout=900)
        sem = asyncio.Semaphore(concurrency)

        async def one(m: str, pdf: Path, run: int) -> None:
            suffix = "" if run == 1 else f".r{run}"
            target = out / schema / m / f"{pdf.stem}{suffix}.json"
            if target.is_file() and "error" not in json.loads(target.read_text()):
                return
            async with sem:
                try:
                    record = await _extract_one(client, m, schema, pdf)
                    status = f"{len(record['result']['references'])} refs, {record['seconds']}s"
                except Exception as exc:  # noqa: BLE001 — recorded per PDF, the run continues
                    record = {"model": m, "pdf": pdf.name, "error": f"{type(exc).__name__}: {exc}"}
                    status = record["error"][:200]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(record, indent=1, ensure_ascii=False))
            print(f"{schema} {m:18} {pdf.stem}{suffix:4} {status}", flush=True)

        try:
            await asyncio.gather(*(one(m, p, k) for m in model for p in pdfs
                                   for k in range(1, repeat + 1)))
        finally:
            await client.close()

    asyncio.run(go())


@app.command()
def grobid(
    pdfs: list[Path] = typer.Argument(...),
    out: Path = typer.Option(DEFAULT_OUT / "grobid"),
    url: str = typer.Option("http://localhost:8070"),
) -> None:
    """Fetch GROBID TEI for each PDF with the pipeline's own client (cached as <stem>.tei.xml)."""
    from reference_audit.pdf.grobid import GrobidClient, GrobidError

    async def go() -> None:
        client = GrobidClient(url)
        try:
            for pdf in pdfs:
                target = out / f"{pdf.stem}.tei.xml"
                if target.is_file():
                    continue
                try:
                    tei = await client.fulltext_tei(pdf)
                except GrobidError as exc:
                    tei = None
                    (out / f"{pdf.stem}.error.txt").write_text(str(exc))
                    print(f"{pdf.stem}: {exc}")
                if tei is not None:
                    out.mkdir(parents=True, exist_ok=True)
                    target.write_text(tei, encoding="utf-8")
        finally:
            await client.aclose()

    asyncio.run(go())


# =================================================================================================
# Extraction → BibEntry
# =================================================================================================

_BIB_TYPE = {"thesis": "phdthesis", "report": "techreport"}


def reference_to_entry(key: str, ref: dict) -> BibEntry:
    """One extracted (or ground-truth) reference → the `BibEntry` the auditor would receive."""
    fields: dict[str, str] = {}
    for name, value in (
        ("title", ref.get("title")), ("journal", ref.get("venue")),
        ("publisher", ref.get("publisher")), ("volume", ref.get("volume")),
        ("number", ref.get("issue")), ("pages", ref.get("pages")), ("doi", ref.get("doi")),
        ("isbn", ref.get("isbn")), ("url", ref.get("url")),
        ("year", str(ref["year"]) if ref.get("year") else None),
    ):
        if value:
            fields[name] = str(value)
    if ref.get("arxiv_id"):
        fields["eprint"] = ref["arxiv_id"]
        fields["archiveprefix"] = "arXiv"
    fields["raw"] = ref.get("raw_text") or ref.get("raw") or ""
    bib_type = _BIB_TYPE.get(ref.get("entry_type") or "misc", ref.get("entry_type") or "misc")
    return entry_from_fields(key, bib_type, fields, authors=list(ref.get("authors") or []))


def load_extraction(path: Path) -> tuple[dict, list[BibEntry]] | None:
    """(record, entries), or None when the call failed — a failure is counted, never scored as 0."""
    record = json.loads(path.read_text())
    if "error" in record:
        return None
    refs = record["result"]["references"]
    return record, [reference_to_entry(f"r{i + 1}", r) for i, r in enumerate(refs)]


# =================================================================================================
# Scoring
# =================================================================================================


def _norm_pages(p: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (p or "").lower().replace("–", "-").split("-")[0])


def _norm_url(u: str | None) -> str:
    return re.sub(r"^https?://(www\.)?", "", (u or "").strip().lower()).rstrip("/")


def _first_family(e: BibEntry) -> str:
    return last_name(e.authors[0]).lower() if e.authors else ""


def _tier(truth: BibEntry, got: BibEntry) -> str | None:
    """`pdf_oracle._tier`, plus two tiers for references that print no title: a bare URL, and the
    author/year/pages styles common in chemistry."""
    tier = _ORACLE_TIER(truth, got)
    if tier is not None:
        return tier
    if not truth.title and truth.ids.url and _norm_url(truth.ids.url) == _norm_url(got.ids.url):
        return "url"
    if not truth.title and _first_family(truth) and _first_family(truth) == _first_family(got):
        same_year = truth.year is not None and truth.year == got.year
        same_loc = bool(truth.pages) and _norm_pages(truth.pages) == _norm_pages(got.pages)
        if same_year and (same_loc or not truth.pages):
            return "author+year+pages"
    return None


def _score(truth: BibEntry, got: BibEntry) -> float:
    if not truth.title:
        return 0.5 + 0.5 * author_overlap(truth.authors, got.authors)
    return title_ratio(truth.title, got.title) * (0.5 + 0.5 * author_overlap(truth.authors, got.authors))


def compare(truth: list[BibEntry], extracted: list[BibEntry]):
    """`pdf_oracle.compare` with the title-less tier: same greedy one-to-one matching."""
    pdf_oracle._tier, pdf_oracle._score = _tier, _score
    try:
        return pdf_oracle.compare(truth, extracted)
    finally:
        pdf_oracle._tier, pdf_oracle._score = _ORACLE_TIER, _ORACLE_SCORE


class FieldTally(BaseModel):
    """Per-field outcomes over matched pairs. `wrong` = both present and different (the dangerous
    case); `missing` = printed but not extracted; `invented` = extracted but not printed."""

    correct: Counter = Field(default_factory=Counter)
    wrong: Counter = Field(default_factory=Counter)
    missing: Counter = Field(default_factory=Counter)
    invented: Counter = Field(default_factory=Counter)
    examples: dict[str, list[str]] = Field(default_factory=lambda: defaultdict(list))

    model_config = {"arbitrary_types_allowed": True}

    def add(self, field: str, truth, got, same: bool, label: str) -> None:
        if truth and got:
            (self.correct if same else self.wrong)[field] += 1
            if not same and len(self.examples[f"wrong {field}"]) < 12:
                self.examples[f"wrong {field}"].append(f"{label}: printed {truth!r}, got {got!r}")
        elif truth:
            self.missing[field] += 1
        elif got:
            self.invented[field] += 1
            if len(self.examples[f"invented {field}"]) < 12:
                self.examples[f"invented {field}"].append(f"{label}: got {got!r}")


def tally_fields(pairs, tally: FieldTally, doc: str) -> None:
    for p in pairs:
        t, g = p.truth, p.got
        label = f"{doc}#{p.truth_index + 1}"
        tally.add("title", t.title, g.title, title_ratio(t.title, g.title) >= 0.95, label)
        tally.add("year", t.year, g.year, t.year == g.year, label)
        tally.add("doi", t.ids.doi, g.ids.doi, t.ids.doi == g.ids.doi, label)
        tally.add("arxiv", t.ids.arxiv_id, g.ids.arxiv_id, t.ids.arxiv_id == g.ids.arxiv_id, label)
        # The author *count*, not the names: name-format differences (Vancouver "Chen S", compound
        # surnames) make a name comparison noisy, while a wrong count is always a real error — a
        # dropped author, or GROBID shifting given names onto the next person.
        tally.add("author count", len(t.authors), len(g.authors),
                  len(t.authors) == len(g.authors), label)


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({n / d:.1%})" if d else f"{n}/{d}"


def _usage_line(records: list[dict]) -> str:
    if not records:
        return ""
    tin = sum(r["usage"].get("input_tokens", 0) for r in records)
    tout = sum(r["usage"].get("output_tokens", 0) for r in records)
    reason = sum((r["usage"].get("output_tokens_details") or {}).get("reasoning_tokens", 0)
                 for r in records)
    secs = sorted(r["seconds"] for r in records)
    return (f"tokens in {tin:,} / out {tout:,} (reasoning {reason:,}); "
            f"latency median {secs[len(secs) // 2]}s, max {secs[-1]}s over {len(records)} calls")


def _llm_extractors(models: list[str], schemas: list[str]) -> list[str]:
    """Extractor names `<model>@<schema>`, one column per (schema, model) in the score tables."""
    return [f"{m}@{sch}" for sch in schemas for m in models]


def _extraction_path(extractions: Path, extractor: str, stem: str) -> Path:
    model, schema = extractor.split("@")
    return extractions / schema / model / f"{stem}.json"


_TEXT_LAYERS: dict[Path, tuple[str, str]] = {}


def _text_layer(pdf: Path) -> tuple[str, str]:
    """The PDF's text layer with all whitespace removed, lowercased — once as is and once with
    line-break hyphens dropped, so an identifier split across lines is found either way."""
    if pdf not in _TEXT_LAYERS:
        import subprocess

        text = subprocess.run(["pdftotext", "-q", str(pdf), "-"], check=True,
                              capture_output=True, text=True).stdout.lower()
        _TEXT_LAYERS[pdf] = (re.sub(r"\s+", "", text), re.sub(r"-\s*\n\s*|\s+", "", text))
    return _TEXT_LAYERS[pdf]


def doi_guard(pdf: Path, pairs, spurious) -> tuple[int, int, list[str]]:
    """The deterministic check a production version would run: every extracted DOI must occur in
    the PDF's own text layer. Returns (DOIs checked, DOIs not found, which of those were wrong).

    It cannot see a DOI that is printed but mis-joined *and* still present (a kept line-break
    hyphen), and it does not need to see the model's reasoning: a DOI absent from the page is
    reported, never used.
    """
    plain, dehyphenated = _text_layer(pdf)
    checked, absent, verdicts = 0, 0, []
    for got, truth in [(p.got, p.truth) for p in pairs] + [(g, None) for g in spurious]:
        doi = got.ids.doi
        if not doi:
            continue
        checked += 1
        if doi in plain or doi in dehyphenated:
            continue
        absent += 1
        correct = truth is not None and truth.ids.doi == doi
        verdicts.append(f"{doi} ({'correct DOI' if correct else 'WRONG DOI'})")
    return checked, absent, verdicts


def shared_dois(entries: list[BibEntry]) -> list[str]:
    """DOIs that two entries with different titles both carry — the signature of one entry's tail
    spliced onto another (luna once copied a later entry's venue, year and DOI into an earlier
    one). A printed bibliography can do this too; either way it is a finding, not a value to use.
    """
    by_doi: dict[str, list[BibEntry]] = defaultdict(list)
    for e in entries:
        if e.ids.doi:
            by_doi[e.ids.doi].append(e)
    return [doi for doi, group in by_doi.items()
            if any(title_ratio(group[0].title, other.title) < 0.9 for other in group[1:])]


def _doubts(record: dict, pairs, label: str) -> list[str]:
    """The per-entry doubts of a v2 extraction (none for v1), each tagged with the printed entry it
    belongs to, so they can be read against the errors the scorer found."""
    refs = record["result"]["references"]
    return [f"{label}#{p.truth_index + 1}: {d}"
            for p in pairs for d in refs[int(p.got.key[1:]) - 1].get("doubts") or []]


@app.command()
def roundtrip(
    extractions: Path = typer.Option(DEFAULT_OUT / "extractions"),
    model: list[str] = typer.Option(list(DEFAULT_MODELS)),
    schema: list[str] = typer.Option(list(SCHEMAS)),
) -> None:
    """Score LLM extraction of the compiled test documents against their `.bib`, next to GROBID."""
    from pdf_oracle import ground_truth

    docs = sorted((ROOT / "tests" / "documents").glob("*/.build/*/*.pdf"))
    for pdf in docs:
        build = pdf.parent
        doc_dir = build.parent.parent
        version = build.name
        bib = _document_bib(doc_dir, version)
        truth = ground_truth(bib, build / f"{version}.bbl")
        label = f"{doc_dir.name}/{version}"
        tei = build / f"{version}.tei.xml"
        rows = [("grobid", compare(truth, parse_tei(tei.read_text(encoding="utf-8")).entries), None)]
        for ex in _llm_extractors(model, schema):
            path = _extraction_path(extractions, ex, _roundtrip_stem(doc_dir.name, version))
            loaded = load_extraction(path) if path.is_file() else None
            if loaded is None:
                print(f"--- {label} [{ex}] --- no extraction ({path})")
                continue
            record, entries = loaded
            rows.append((ex, compare(truth, entries), record))
        for name, result, record in rows:
            print(result.table(f"{label} [{name}]"))
            tally = FieldTally()
            tally_fields(result.pairs, tally, label)
            print(f"  title wrong            : {tally.wrong['title']}   author count differs: "
                  f"{tally.wrong['author count']} (vs a .bib, mostly `others` / joint organizations)")
            if record or name == "grobid":
                checked, absent, verdicts = doi_guard(pdf, result.pairs, result.spurious)
                print(f"  DOIs not in text layer : {absent}/{checked}  {'; '.join(verdicts)}")
                shared = shared_dois([p.got for p in result.pairs] + result.spurious)
                print(f"  DOI on 2+ titles       : {', '.join(shared) or 'none'}")
            if record:
                print("  " + _usage_line([record]))
                if record.get("schema") == "v2":
                    doubts = _doubts(record, result.pairs, label)
                    print(f"  doubts raised          : {len(doubts)}")
                    for line in doubts[:8]:
                        print(f"      doubt: {line[:160]}")
            for kind, ex in tally.examples.items():
                for line in ex[:5]:
                    print(f"      {kind}: {line[:160]}")
        print()


def _document_bib(doc_dir: Path, version: str) -> Path:
    from conftest import DocumentVersion

    return DocumentVersion(doc_dir.name, version).bib


def _roundtrip_stem(slug: str, version: str) -> str:
    return f"{slug}--{version}"


@app.command("roundtrip-pdfs")
def roundtrip_pdfs(link_dir: Path = typer.Option(DEFAULT_OUT / "roundtrip_pdfs")) -> None:
    """Symlink the compiled test PDFs under unique names, for `extract`."""
    link_dir.mkdir(parents=True, exist_ok=True)
    for pdf in sorted((ROOT / "tests" / "documents").glob("*/.build/*/*.pdf")):
        link = link_dir / f"{_roundtrip_stem(pdf.parent.parent.parent.name, pdf.parent.name)}.pdf"
        if not link.exists():
            link.symlink_to(pdf)
        print(link)


@app.command()
def ai4x(
    sample: Path = typer.Option(..., help="JSON {pdf name: {stratum, producer, ...}}."),
    gt: Path = typer.Option(..., help="Directory of ground-truth <stem>.json files."),
    extractions: Path = typer.Option(DEFAULT_OUT / "extractions"),
    grobid_dir: Path = typer.Option(DEFAULT_OUT / "grobid"),
    model: list[str] = typer.Option(list(DEFAULT_MODELS)),
    schema: list[str] = typer.Option(list(SCHEMAS)),
    pdf_dir: Path | None = typer.Option(None, help="The sample's PDFs, for the DOI text-layer guard."),
    report: Path | None = typer.Option(None, help="Also write the per-PDF table as JSON here."),
) -> None:
    """Score extractors on third-party PDFs against hand-built ground truth, per stratum."""
    meta = json.loads(sample.read_text())
    llm_extractors = _llm_extractors(model, schema)
    extractors = ["grobid", *llm_extractors]
    doubts: dict[str, list[str]] = defaultdict(list)
    guard: dict[str, list] = defaultdict(lambda: [0, 0, []])
    per_pdf: list[dict] = []
    agg: dict[tuple[str, str], dict] = {}
    tallies: dict[str, FieldTally] = {e: FieldTally() for e in extractors}
    records: dict[str, list[dict]] = defaultdict(list)

    for name, m in sorted(meta.items(), key=lambda kv: (kv[1]["stratum"], kv[0])):
        stem = Path(name).stem
        truth_doc = json.loads((gt / f"{stem}.json").read_text())
        truth = [reference_to_entry(f"t{i + 1}", r) for i, r in enumerate(truth_doc["references"])]
        for ex in extractors:
            row = {"pdf": stem, "stratum": m["stratum"], "producer": m["producer"],
                   "extractor": ex, "printed": len(truth)}
            if ex == "grobid":
                tei = grobid_dir / f"{stem}.tei.xml"
                if not tei.is_file():
                    row["failed"] = (grobid_dir / f"{stem}.error.txt").read_text()[:200] \
                        if (grobid_dir / f"{stem}.error.txt").is_file() else "no TEI"
                    entries = None
                else:
                    entries = parse_tei(tei.read_text(encoding="utf-8")).entries
            else:
                path = _extraction_path(extractions, ex, stem)
                loaded = load_extraction(path) if path.is_file() else None
                if loaded is None:
                    row["failed"] = (json.loads(path.read_text()).get("error", "?")[:200]
                                     if path.is_file() else "not extracted")
                    entries = None
                else:
                    record, entries = loaded
                    records[ex].append(record)
                    row["has_reference_list"] = record["result"]["has_reference_list"]
                    if ex.endswith("@v3"):
                        row["list_problems"] = list_problems(
                            PrintedReferenceList.model_validate(record["result"]))
                    row["problems"] = record["result"]["problems"]
            if entries is not None:
                result = compare(truth, entries)
                tally_fields(result.pairs, tallies[ex], stem)
                row.update(extracted=len(entries), matched=result.matched,
                           spurious=len(result.spurious),
                           missed=[e.raw_fields.get("raw", e.title)[:100] for e in result.unmatched_truth],
                           spurious_items=[(e.title or e.raw_fields.get("raw", "")
                                            or e.raw_fields.get("pdf_raw", ""))[:100]
                                           for e in result.spurious],
                           doi_fabricated=result.doi_fabricated, doi_wrong=result.doi_wrong,
                           doi_unexpected=result.doi_unexpected, year_wrong=result.year_wrong)
                if ex != "grobid":
                    doubts[ex] += _doubts(record, result.pairs, stem)
                if pdf_dir is not None:
                    checked, absent, verdicts = doi_guard(pdf_dir / name, result.pairs,
                                                          result.spurious)
                    guard[ex][0] += checked
                    guard[ex][1] += absent
                    guard[ex][2] += [f"{stem}: {v}" for v in verdicts]
                guard[ex].append([f"{stem}: {d}" for d in shared_dois(entries)])
            per_pdf.append(row)
            for key in ((m["stratum"], ex), ("ALL", ex)):
                a = agg.setdefault(key, Counter())
                a["pdfs"] += 1
                if "failed" in row:
                    a["failed"] += 1
                    continue
                for k in ("printed", "extracted", "matched", "spurious", "doi_fabricated",
                          "year_wrong"):
                    a[k] += row[k]
                if row["printed"] == 0:
                    a["no_list_pdfs"] += 1
                    a["no_list_pdfs_clean"] += row["extracted"] == 0
                elif row["matched"] == row["printed"] and row["spurious"] == 0:
                    a["exact_pdfs"] += 1

    print("=== AI4X sample: reference-list extraction vs hand-built ground truth ===")
    for stratum in sorted({k[0] for k in agg}, key=lambda s: (s == "ALL", s)):
        print(f"\n--- {stratum} ---")
        for ex in extractors:
            a = agg.get((stratum, ex))
            if not a:
                continue
            with_list = a["pdfs"] - a["failed"] - a["no_list_pdfs"]
            print(
                f"  {ex:15} pdfs {a['pdfs']:2} failed {a['failed']}  "
                f"recall {_pct(a['matched'], a['printed'])}  "
                f"spurious {a['spurious']}  exact {a['exact_pdfs']}/{with_list}  "
                f"no-list clean {a['no_list_pdfs_clean']}/{a['no_list_pdfs']}  "
                f"DOI fabricated {a['doi_fabricated']}  year wrong {a['year_wrong']}"
            )
    print("\n--- field accuracy over matched pairs (correct / wrong / missing / invented) ---")
    for ex in extractors:
        t = tallies[ex]
        cells = [f"{f} {t.correct[f]}/{t.wrong[f]}/{t.missing[f]}/{t.invented[f]}"
                 for f in ("title", "author count", "year", "doi", "arxiv")]
        print(f"  {ex:15} " + "  ".join(cells))
    for ex in llm_extractors:
        print(f"  {ex:15} " + _usage_line(records[ex]))
    if pdf_dir is not None:
        print("\n--- DOI text-layer guard (extracted DOIs absent from the PDF's own text) ---")
        for ex in extractors:
            checked, absent, verdicts, *shared = guard[ex]
            print(f"  {ex:15} {absent}/{checked}  {'; '.join(verdicts)}")
            flat = [d for group in shared for d in group]
            print(f"  {'':15} DOI on 2+ titles: {', '.join(flat) or 'none'}")
    v3_rows = [r for r in per_pdf if r["extractor"].endswith("@v3") and "failed" not in r]
    if v3_rows:
        print("\n--- v3 list checks (a complete list must pass; each failure would be escalated) ---")
        for ex in llm_extractors:
            if ex.endswith("@v3"):
                rows = [r for r in v3_rows if r["extractor"] == ex]
                bad = [f"{r['pdf']}: {'; '.join(r['list_problems'])}" for r in rows
                       if r.get("list_problems")]
                print(f"  {ex:15} {len(bad)}/{len(rows)} rejected  {' | '.join(bad)}")
    print("\n--- v2/v3 doubts (per-entry, as raised by the model) ---")
    for ex in llm_extractors:
        if ex.endswith(("@v2", "@v3")):
            print(f"  {ex:15} {len(doubts[ex])} doubts")
            for line in doubts[ex]:
                print(f"      {line[:170]}")
    print("\n--- per-PDF (printed / extracted / matched / spurious) ---")
    by_pdf: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in per_pdf:
        by_pdf[row["pdf"]][row["extractor"]] = row
    for stem, rows in by_pdf.items():
        first = next(iter(rows.values()))
        cells = []
        for ex in extractors:
            r = rows.get(ex, {})
            cells.append(f"{ex}: " + ("FAILED" if "failed" in r else
                                      f"{r['extracted']:>2}/{r['matched']:>2}/{r['spurious']}"))
        print(f"  {stem} {first['stratum']:13} {first['producer']:5} printed {first['printed']:>2}  "
              + "  ".join(cells))
    print("\n--- examples of field errors ---")
    for ex in extractors:
        for kind, lines in tallies[ex].examples.items():
            for line in lines[:6]:
                print(f"  [{ex}] {kind}: {line[:170]}")
    if report:
        report.write_text(json.dumps(per_pdf, indent=1, ensure_ascii=False))


def _oracle_artifact(truth: BibEntry, got: BibEntry) -> bool:
    """An author-count difference that is the `.bib` oracle's, not the extractor's: `and others` in
    the `.bib` (printed "et al."), or two organizations printed as one "X and Y" author."""
    return (any(a.strip().lower() == "others" for a in truth.authors)
            or any(" and " in a for a in got.authors))


@app.command()
def runs(
    extractions: Path = typer.Option(DEFAULT_OUT / "extractions"),
    model: list[str] = typer.Option(..., help="Model specs to compare, e.g. gpt-6-luna:high."),
    schema: str = typer.Option("v2"),
) -> None:
    """One line per run on each roundtrip document: every field error, minus oracle artifacts, and
    what the two deterministic DOI checks flag. For measuring run-to-run variance."""
    from pdf_oracle import ground_truth

    for pdf in sorted((ROOT / "tests" / "documents").glob("*/.build/*/*.pdf")):
        build, doc_dir = pdf.parent, pdf.parent.parent.parent
        stem = _roundtrip_stem(doc_dir.name, build.name)
        truth = ground_truth(_document_bib(doc_dir, build.name), build / f"{build.name}.bbl")
        print(f"--- {doc_dir.name}/{build.name} ({len(truth)} printed) ---")
        for m in model:
            paths = sorted((extractions / schema / m).glob(f"{stem}*.json"))
            for path in paths:
                loaded = load_extraction(path)
                if loaded is None:
                    print(f"  {m:18} {path.stem[len(stem):] or '.r1':5} FAILED")
                    continue
                record, entries = loaded
                result = compare(truth, entries)
                errors = []
                for p in result.pairs:
                    t, g, n = p.truth, p.got, p.truth_index + 1
                    if t.ids.doi and g.ids.doi and t.ids.doi != g.ids.doi:
                        errors.append(f"#{n} DOI {g.ids.doi}")
                    if g.ids.doi and not t.ids.doi:
                        errors.append(f"#{n} unexpected DOI {g.ids.doi}")
                    if t.year and g.year and t.year != g.year:
                        errors.append(f"#{n} year {g.year}≠{t.year}")
                    if t.title and title_ratio(t.title, g.title) < 0.95:
                        errors.append(f"#{n} title")
                    if len(t.authors) != len(g.authors) and not _oracle_artifact(t, g):
                        errors.append(f"#{n} authors {len(g.authors)}≠{len(t.authors)}")
                errors += [f"missed {e.key}" for e in result.unmatched_truth]
                errors += [f"spurious '{e.title[:40]}'" for e in result.spurious]
                if schema == "v3":
                    parsed = PrintedReferenceList.model_validate(record["result"])
                    errors += [f"list check: {p}" for p in list_problems(parsed)]
                *_, verdicts = doi_guard(pdf, result.pairs, result.spurious)
                shared = shared_dois(entries)
                usage = record["usage"]
                reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
                print(f"  {m:18} {path.stem[len(stem):] or '.r1':5} "
                      f"{len(errors)} errors, {record['seconds']:.0f}s, out {usage['output_tokens']:,} "
                      f"(reasoning {reasoning:,})")
                for e in errors:
                    print(f"      {e}")
                for v in verdicts:
                    print(f"      guard: not in text layer: {v}")
                for d in shared:
                    print(f"      guard: DOI on 2+ titles: {d}")


if __name__ == "__main__":
    app()
