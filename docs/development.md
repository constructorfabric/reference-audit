# Development

Clone the repository and install the project with its development dependencies:

```bash
git clone https://github.com/constructorfabric/reference-audit.git
cd reference-audit
uv sync
```

Inside the clone, run the tool as `uv run reference-audit …`.

```bash
uv run pytest          # unit + integration tests (databases & LLM are mocked)
REFERENCE_AUDIT_LIVE=1 uv run pytest -m clickhouse   # the live check of the local ClickHouse mirror
REFERENCE_AUDIT_HALLMARK_LIVE=1 uv run pytest tests/test_hallmark_live.py -q # full public corpus
cfs validate           # validate the governance artifacts and code traceability
```

The unit and integration tests mock all database and LLM calls, so they are fast and offline. The
live tests (markers `clickhouse`, `grobid`, `pdf_build`) are not deselected by default: they run when
their service or toolchain is reachable and skip otherwise, unless `REFERENCE_AUDIT_LIVE=1` makes them
required. Use `USE_LLM=false uv run pytest -m "not clickhouse and not grobid and not pdf_build and not hallmark_live"`
for a strictly offline run, also disabling live LLM settings inherited from the settings files (see [Configuration](configuration.md)). When the tool gets a case
wrong, the fix is captured as a new test with the recorded response (see
[`architecture/SPEC.md`](../architecture/SPEC.md), "General Design Principles").

The suite includes all 1,950 HALLMARK v1.2 `dev_public` and `test_public` examples, copied into
[`tests/fixtures/hallmark/v1.2`](../tests/fixtures/hallmark/v1.2/README.md) with commit/checksum provenance,
credit to Patrik Reizinger and Wieland Brendel, and the original MIT license. HALLMARK is not a test
dependency. Every record exercises parsing offline, including nine malformed records that must be
reported without hiding their neighbours. Twelve reviewed cases have independent recorded-source
regressions. Original data and label reviews stay separate from `native_expectations.json`, which
expresses evidence-backed identity and field expectations in reference-audit's vocabulary. The
`hallmark_live` tests require the separate opt-in above (network + LLM, potentially an hour or more).
They check the native reporting contract: reasoned unresolved/failed/multiple outcomes and unverified
metadata are legitimate results; unexplained outcomes, unsupported venue errors and violations of
independent expectations fail. Binary label comparisons, including disputed labels, are diagnostics
in `diagnostics.json`, not assertions. Records without an independent oracle get consistency and
reporting checks only; a passing run does not mean independently proven corpus-wide accuracy.
Use `-s` to see result counts and output paths, or set `REFERENCE_AUDIT_HALLMARK_CACHE` to reuse a
source/LLM cache (whole-entry verdicts still require the current pipeline version).

## The PDF extraction oracle

Extraction fidelity is measured rather than assumed. Each test document is compiled to PDF from its own
`.tex` + `.bib`, so that `.bib` is ground truth for whatever GROBID reads back out; extracted references
are matched to printed ones by identifier and normalized title (there are no shared keys), and the
results are asserted against pinned floors.

These tests need a LaTeX toolchain and a running GROBID. When either is missing they **skip** with a
message naming what to start; with `REFERENCE_AUDIT_LIVE=1` they are **required**, and a missing
dependency fails instead of skipping — so a broken setup cannot sit green indefinitely.

```bash
uv run python tests/pdf_fixtures.py --all --preflight-only    # what would a compile need?
uv run python tests/pdf_fixtures.py --all --compile           # build the PDFs
REFERENCE_AUDIT_LIVE=1 uv run pytest -k pdf                   # measure, then assert the floors
```

Build artifacts land in a gitignored `tests/documents/<slug>/.build/<version>/`; nothing is written
next to the fixtures. The committed oracle is GROBID's TEI XML for the smallest document
(`tests/fixtures/tei/`, with a `.provenance.txt` recording the GROBID image, request parameters and
TeX Live version) — never a PDF. Regenerate it with the command recorded in that `.provenance.txt`
(`--slug … --version … --compile --grobid <url> --write-tei`). The one committed PDF,
`tests/fixtures/embedded/paper.pdf`, is not a GROBID oracle: it is compiled from the `.tex` + `.bib`
beside it with the AI4X template's `ai4x.cls` and `ai4x.bst`, because what it tests is the attachment
only a template-built PDF has.

Two deliberate behaviors of the harness:

- **Missing source files are never stubbed.** A compile that lacks an `\input` target, `.sty` or `.bst`
  fails with *every* missing file listed at once, so they can be supplied in one pass. Each document's
  `SOURCES.md` records where its auxiliary files came from.
- **Missing figures are the one substitution**, since a bibliography does not depend on them. They are
  replaced by generated placeholder images, and that substitution is stated in the build report and in
  the recorded fixture's provenance — page layout differs from the published article, which does not
  affect reference extraction.

The pinned numbers are a **regression floor for the recorded GROBID image and bibliography styles, not
a fidelity claim for arbitrary PDFs**: these bibliographies were typeset by BibTeX from clean data,
which is materially easier than what real-world PDFs contain.

## Benchmarking on HALLMARK

Verdict accuracy is measured on [HALLMARK](https://github.com/rpatrik96/hallmark), a
citation-hallucination benchmark, by `benchmarks/hallmark_bench.py`.
[`benchmarks/README.md`](../benchmarks/README.md) covers the setup, the `audit` / `score` / `submit`
subcommands, the verdict-to-label mappings, the results so far, and how to run on the withheld
`test_hidden` split. A `contradiction` maps to HALLUCINATED under `strict` and UNCERTAIN under
`identity`, because the citation cannot be treated as a clean identification.

## Project layout

```
src/reference_audit/
  parsing/     # .bib / .tex / identifier parsing; latex.py = LaTeX → Unicode for .bib values;
               #   entities.py = HTML character references; context.py = the shared sentence definition
  pdf/         # PDF input: grobid.py (the only network module — one client, one named error per
               #   failure mode) + tei.py (pure TEI -> BibEntry / CitationContext mapping) +
               #   embedded.py (the BibTeX records a PDF embeds, and the printed-title check)
  sources/     # modular adapters: Crossref, OpenAlex, Semantic Scholar, arXiv, DBLP, Open Library,
               #   Google Books, publisher (DOI landing-page citation export), web (cited-page fetch),
               #   render (headless-browser rendering of JS single-page-app pages); registry.py =
               #   routing; base.py = the adapter contract; normalize.py = raw JSON → SourceRecord;
               #   http.py = per-source spacing, in-flight caps and retries;
               #   clickhouse.py = the local-mirror backend for S2/OpenAlex/DBLP; titlewords.py = the
               #   searchable words of a title (shared by every all-words title search)
  matching/    # candidate pooling, feature scoring, author-name matching (names.py), LLM
               #   adjudication of undecided candidates, SAME-OBJECT clustering, verdicts, web check
  llm/         # OpenAI structured output (pydantic schemas): adjudication, field and alignment prompts
  cache/       # SQLite memoization of DB/LLM calls (errors never cached)
  alignmentcheck.py # --check-citations: citing context vs. the cited work's abstract
  bookcheck.py # Open Library edition resolution (cited vs. latest edition)
  fieldcheck.py# per-field correctness / formatting findings for an exactly-one match
  venuecheck.py# bounded artifact / complete-catalogue evidence for venue contradictions
  versioning.py# better-version detection (published > preprint)
  pipeline.py  # async orchestration
  report.py    # text / JSON rendering
  config.py    # AuditConfig (model, keys, thresholds) and the settings files it reads
  models.py    # pydantic domain models
  inputs.py    # which input shape the CLI arguments name (a .bib, optionally after its .tex, or a .pdf)
  cli.py       # command-line entry point (Typer)
architecture/  # governed specification & design (SPEC, PRD, DESIGN, DECOMPOSITION, features)
docs/          # the user and developer documentation (README.md links to each page)
  archive/     # results of past experiments, kept as they were
  ideas/       # ideas not yet tried
benchmarks/
  README.md    # the HALLMARK benchmark: setup, subcommands, mappings, results, test_hidden
  hallmark_bench.py # HALLMARK harness: audit a blind split, map verdicts to labels, run HALLMARK's
                    #   evaluator, write submissions (needs the pinned checkout in benchmarks/.hallmark)
  submissions/hallmark/ # HALLMARK submission files (predictions JSONL + manifest), one per split
  hallmark_label_errors.md # dev_public entries labelled VALID that are hallucinated, with evidence
tests/
  documents/   # test papers: <paper-title-slug>/<version>.{tex,bib} (initial, polished, …),
               #   plus the auxiliary LaTeX sources needed to compile them (see each SOURCES.md)
  fixtures/tei/# recorded GROBID output + provenance, so the mapper is verifiable offline
  fixtures/embedded/ # an AI4X-template PDF that embeds its references' BibTeX records, + its sources
  fixtures/dblp/     # a recorded DBLP SPARQL response + provenance
  fixtures/hallmark/v1.2/ # copied public corpus, MIT license, provenance, label reviews, source snapshots
  hallmark_cases.py # validated corpus loader; strips labels before an audit
  test_hallmark_corpus.py # offline parsing of every public example + independent-source corrections
  test_hallmark_live.py # opt-in full-pipeline public-corpus tests (network + LLM)
  pdf_fixtures.py # not a test: preflight + compile + TEI capture, runnable by hand
  pdf_oracle.py   # matches extracted references to printed ones and scores the difference
  *.py         # mocked unit/integration tests, plus the live ClickHouse / PDF tests (skipped when
               #   their service is missing); the pilot paper is the development oracle
```

See [`architecture/SPEC.md`](../architecture/SPEC.md) for the full specification and the
`architecture/` artifacts for the detailed design.
