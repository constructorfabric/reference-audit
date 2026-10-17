# Usage

Three input forms — a bibliography on its own, a manuscript with its bibliography, or a PDF on its
own:

```bash
reference-audit audit <references.bib> [options]
reference-audit audit <main.tex> <references.bib> [options]
reference-audit audit <paper.pdf> [options]
```

(From a clone of the repository rather than an installed tool, prefix the commands with `uv run`.)

They are mutually exclusive: a PDF *is* the whole input, since its reference list is read from it (or
the PDF embeds its references' BibTeX records), so passing a `.bib` alongside one is rejected. PDF input
needs the OpenAI API (a language model reads the references), or a running GROBID with
`--pdf-extractor grobid`; see [Auditing a PDF](pdf-input.md).

The `.tex` is optional. Every entry of the `.bib` is identified and checked either way; the
manuscript adds:

- **citation bookkeeping**: which entries are cited, which are never cited, and which `\cite` keys
  have no `.bib` entry (following `\input`/`\include` and honouring `\nocite{*}`);
- **citing sentences** for `--check-citations`.

Without it, the report says `citedness unknown` instead of cited/uncited counts, and an `INPUT NOTES`
line states what was not checked. With `--check-citations`, a second note says alignment was not
checked.

Example, against the pilot paper bundled with the repository (run from a clone):

```bash
uv run reference-audit audit \
  tests/documents/directing-open-ended-evolution/initial.tex \
  tests/documents/directing-open-ended-evolution/initial.bib
```

```
Reference audit
  28 entries  ·  28 cited  ·  0 uncited  ·  7 with issues  ·  1 commented twins
  verdicts: 28 matched  ·  0 no-match  ·  0 ambiguous  ·  0 unresolved
  types: article=18, book=2, inproceedings=5, misc=3

CAPITAL OFFENCES — No hallucinated citations

UNABLE TO VERIFY — No unresolved references

ISSUES (7) — other problems to review:

[article] wolpert2007  (cited)
    Using self-dissimilarity to quantify complexity
    ids: doi:10.1002/cplx.20165
    ⚠ DOI normalized from URL form ('https://doi.org/10.1002/cplx.20165' → '10.1002/cplx.20165')
    ✓ exactly one match (high) — Matched a single work via crossref.
      matched: doi:10.1002/cplx.20165  (crossref)

[inproceedings] fu2023dreamsim  (cited)
    DreamSim: Learning New Dimensions of Human Visual Similarity using Synthetic Data
    ids: (no identifier)
    ⚠ no DOI/arXiv id (will attempt DOI backfill)
    ✓ exactly one match (high) — Matched a single work via semantic_scholar (2 versions).
...

FORMATTING NITS & ADVISORIES (3) — cosmetic field fixes, citation or reading notes:

[article] goldenfeld1992lectures  (cited)
    Lectures on Phase Transitions and the Renormalization Group
    ids: doi:10.1201/9780429493492
    · formatting nit in 'pages'='185-–197' — non-standard page separator in '185-–197'; use '--' [crossref]
    ✓ exactly one match (high) — Matched a single work via crossref.
      matched: doi:10.1201/9780429493492  (crossref)
...

NO ISSUES (18) — verified, nothing to fix:
...
```

## Options

| Option | Description |
| --- | --- |
| `-f, --format text\|json\|both` | Output format. `json` emits the full structured report (verdicts, candidates, features) — ideal for tooling and AI agents. Default: `text`. |
| `--no-network` | Parse only: identifiers + cited/uncited bookkeeping, no database or LLM calls. |
| `--no-llm` | Formal-only: skip LLM adjudication for fully deterministic output (useful in CI). |
| `--check-citations` | Advisory: for each in-text citation of a matched reference, check the citing context against the cited work's **abstract** (needs the LLM). See [Citation alignment](#citation-alignment---check-citations). |
| `--fresh` | Ignore results cached before this run and re-query everything; new results overwrite the old ones. Nothing is deleted, so other bibliographies sharing the cache DB keep their cached results. |
| `--cache PATH` | Cache DB location. Default: `.reference_audit/cache.db` beside the `.bib` (or the `.pdf`). |
| `--model NAME` | Override the LLM model (default `gpt-6-luna`). |
| `--pdf-extractor llm\|grobid` | Who reads a PDF's references: a language model over the OpenAI API (default), or a local GROBID service, which also links in-text citations (needed for citedness and `--check-citations` on a PDF). See [Auditing a PDF](pdf-input.md). |
| `--grobid URL` | GROBID base URL. Default `http://localhost:8070` (or `GROBID_URL`). Implies `--pdf-extractor grobid` unless another extractor is given. |
| `--ignore-embedded` | For a PDF that embeds the BibTeX records of its cited works, read the references from the typeset text anyway. See [PDFs that embed their references](pdf-input.md#pdfs-that-embed-their-references). |
| `--backend api\|clickhouse` | Where Semantic Scholar, OpenAlex and DBLP are read from: their public APIs, or a local ClickHouse mirror. Default `SOURCE_BACKEND`, else `api`. See [Local ClickHouse backend](clickhouse-backend.md). |
| `--fail-on hallucinated\|multiple\|contradiction` | Exit non-zero if any entry gets that verdict — for gating submissions in CI. Any other value is rejected before the audit starts, and so is combining it with `--no-network` (the parse-only report has no verdicts to check). |
| `--partial-authors ignore\|warn\|error` | How to report a cited author list that names only some of the work's authors without `and others`: not at all, as `uncertain` (default), or as an `error`. Default `PARTIAL_AUTHORS`, else `warn`. |

`--no-network` applies to `.bib` input (with or without its `.tex`), and to a PDF that embeds the BibTeX records of its
references. Any other PDF's reference list comes from the OpenAI API or an HTTP call to GROBID, so
there is no offline parse path for it, and combining the two is rejected rather than quietly redefining what the flag
promises.

Example — fail a CI check if any reference looks hallucinated, as JSON:

```bash
reference-audit audit paper.tex refs.bib --format json --fail-on hallucinated
```

## Reading the output

The text report leads with two headline categories:

- **`CAPITAL OFFENCES`** — hallucinated citations: entries that conclusively resolve to **no** real
  document (verdict *no match*). When there are none, the report says so explicitly
  (*No hallucinated citations*).
- **`UNABLE TO VERIFY`** — entries we could **not** conclusively clear of being a hallucination, for
  any reason: a transient network/LLM error, an unfamiliar `.bib` type, a dead link, an
  adjudication left unsettled. This is the catch-all that keeps an inconclusive check from masquerading
  as a clean pass. When it is empty the report says *No unresolved references*.

The remaining entries, including resolved-ID contradictions, are then split into `ISSUES`,
`FORMATTING NITS & ADVISORIES`, and `NO ISSUES`. Per-entry verdict glyphs:

- **`! CONTRADICTION`** — a resolved hard paper ID conflicts with supplied fields. The resolved
  target and conflicting values are shown under ISSUES and in JSON (`kind: "contradiction"`,
  `artifacts`, `contradiction_findings`). It is counted separately from hallucinations and ambiguity.
- **`✓ exactly one match`** — resolved to a single document; the matched identifier and version
  count are shown.
- **`✗ NO MATCH (possible hallucination)`** — nothing real corresponds; treat as a likely
  hallucination.
- **`? MULTIPLE matches`** — ambiguous; the entry matches more than one distinct work.
- **unresolved** (no verdict line) — the tool could not conclude (e.g. a transient API error, or a
  cited web page that is a dead link, a JavaScript app shell no browser could render, or otherwise
  could not be confirmed). Never reported as a hallucination. Each cause is a `⚠` line: usually
  `⚠ unresolved: …`, naming the failed source or the undecided candidates; `⚠ audit failed …` when
  the entry's audit raised; `⚠ no title and no identifier …` when there was nothing to search on.
  The JSON carries the list as `unresolved_reasons`.
- **`⚠` lines** — per-entry issues: a normalized/backfilled identifier, a missing ISBN, a dangling
  citation, etc.
- The header counts cited and uncited entries; the **cited-but-missing** citations (a `\cite` with
  no `.bib` entry) and the **uncited** entries are listed at the end of the report.
- **`UNPARSEABLE .bib ENTRIES`** — entries that could not be read, with their line and the reason:
  an unbalanced brace (most often), another syntax error bibtexparser reports, a reference to an
  undefined `@string`, a duplicate field, no fields at all, or a value whose LaTeX cannot be decoded.
  They are not checked at all, and a `\cite` of one is not counted as missing. The JSON lists them
  under `unparsed`.

## Citation alignment (`--check-citations`)

A reference can be perfectly real yet cited for a claim its source never makes. With
`--check-citations`, for every in-text `\cite` of a reference that resolves to **exactly one** work
(other references get no alignment finding),
the auditor extracts the **citing context** (the sentence attaching the claim to the citation) and
compares it against that work's **abstract**, classifying each citation as:

- **`supported`** — the abstract corroborates the citing claim.
- **`contradicted`** — the abstract asserts the opposite; surfaced as a loud per-citation issue
  (*citation may misrepresent the source*), with the abstract quote.
- **`not_in_abstract`** — the abstract is silent on the claim. An abstract is only a summary, so this
  is **not** flagged as misuse — it is an advisory note (the full text may well support the claim).
- **`unverifiable`** — no abstract was retrievable, or the LLM was unavailable or its call failed.
  Reported, never guessed — a silent/absent abstract is never read as a contradiction.

This is **abstract-only** in v1 (never full text) and **advisory**: it never changes a reference's
identification verdict. The JSON report carries the full per-citation `alignment_findings`
(status, evidence quote, confidence) for tooling and AI agents.
