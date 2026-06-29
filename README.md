# reference-audit

Audit the references in a paper. Given a `.bib` bibliography and the `.tex` that cites it,
`reference-audit` figures out **which real-world document each reference actually points to** and
flags ones that don't match anything real (hallucinated references).

For every entry it returns one of three verdicts:

- **exactly one match** — the reference resolves to a single real document (with its DOI/ISBN/URL,
  and any preprint↔published versions merged).
- **no match** — no real document corresponds to the entry (a likely hallucination).
- **multiple matches** — the entry is ambiguous and matches more than one distinct document.

It also backfills missing DOIs/ISBNs, normalizes malformed identifiers, and reports dangling or
uncited citations.

> Designed for three audiences: authors polishing their bibliography, automated screening of
> submissions for hallucinated references, and AI agents writing papers. The full product
> specification lives in [`architecture/SPEC.md`](architecture/SPEC.md).

## How it works

For each entry the tool runs a funnel that prefers cheap, deterministic evidence and only escalates
to an LLM when needed:

1. **Parse** the `.bib` (and resolve `\cite`/`\nocite` in the `.tex`), normalizing DOIs, ISBNs,
   arXiv ids and OpenAlex Work ids (an `openalex.org/W…` URL becomes a first-class identifier).
2. **Query** multiple scholarly databases — Crossref, OpenAlex, Semantic Scholar, arXiv, Open
   Library — both by identifier and by title/author. A cited OpenAlex Work id is resolved directly
   to that Work (the authoritative key for entries — notably trade books — the other sources miss).
3. **Pool** the results, merging records that are the same work (shared identifier, or a
   preprint↔published version link).
4. **Score** each candidate with interpretable features (title/author/year/venue similarity,
   identifier agreement, and distinct-work signals).
5. **Adjudicate** anything that isn't a clean match with an LLM, asking one record at a time whether
   it can correspond to the entry; a second LLM check decides whether two strong candidates are the
   *same* work.
6. **Verdict** — count the distinct works and report none / exactly one / multiple.

A reference identified only by a URL (a `@misc` blog post, software or project page that no scholarly
database indexes) is verified against the page itself: the tool fetches the URL, checks the page's
own HTML metadata (Open Graph / `citation_*` / `<title>` + authors) against the citation, and falls
back to the LLM when that metadata is missing or inconclusive. A dead link or a page that cannot be
confirmed is reported, never silently passed.

A reference that cites an **OpenAlex Work id** (an `openalex.org/W…` URL, common for books and other
titles the article-centric search and Crossref/Open Library miss) is resolved directly to that Work.
When the Work matches the entry's title and authors it is pinned as the match — the author-supplied
Work id is treated as authoritative identity, so a similar-titled foreign-DOI record is never merged
in and its identifiers are never backfilled onto the entry. A cited Work id whose Work has a
mismatched title/author does not confirm the entry; it is left unresolved and reported.

Results are cached in a local SQLite DB, so re-running on the same `.bib` makes **no** repeat
network or LLM calls. A transient outage never counts as "no match".

## Requirements

- Python 3.14
- [`uv`](https://docs.astral.sh/uv/)
- An OpenAI API key (for the LLM adjudication step)

## Setup

```bash
uv sync
```

Create a `.env` file in the project root with your keys (this file is git-ignored):

```dotenv
# Required for LLM adjudication
OPENAI_API_KEY=sk-...

# Optional — improve coverage / rate limits for the data sources
S2_API_KEY=...
NCBI_API_KEY=...
NASA_ADS_API_KEY=...
CORE_API_KEY=...
PAPER_SEARCH_MCP_UNPAYWALL_EMAIL=you@example.org
```

Only `OPENAI_API_KEY` is needed to run the full pipeline; the data sources used by default
(Crossref, OpenAlex, arXiv, Open Library) require no key. You can also run with no LLM at all
(`--no-llm`, see below).

## Usage

```bash
uv run reference-audit audit <main.tex> <references.bib> [options]
```

Example, against the bundled pilot:

```bash
uv run reference-audit audit \
  tests/documents/directing-open-ended-evolution/initial.tex \
  tests/documents/directing-open-ended-evolution/initial.bib
```

```
Reference audit
  28 entries  ·  26 cited  ·  2 uncited  ·  7 with issues  ·  1 commented twins
  verdicts: 27 matched  ·  0 no-match  ·  0 ambiguous  ·  1 unresolved
  types: article=18, book=2, inproceedings=5, misc=3

CAPITAL OFFENCES — No hallucinated citations

UNABLE TO VERIFY (1) — could not conclusively rule out a hallucination (network/LLM error, unfamiliar entry type, dead link, …):
...

ISSUES (6) — other problems to review:

[article] wolpert2007  (cited)
    Using self-dissimilarity to quantify complexity
    ids: doi:10.1002/cplx.20165
    ⚠ DOI normalized from URL form ('https://doi.org/10.1002/cplx.20165' → '10.1002/cplx.20165')
    ✓ exactly one match (high) — Matched a single work via crossref.

[inproceedings] fu2023dreamsim  (UNCITED)
    DreamSim: Learning New Dimensions of Human Visual Similarity using Synthetic Data
    ids: (no identifier)
    ⚠ no DOI/arXiv id (will attempt DOI backfill)
    ✓ exactly one match (high) — Matched a single work via semantic_scholar (2 versions).
...

FORMATTING NITS (3) — verified; only cosmetic field fixes:

[article] goldenfeld1992lectures  (cited)
    Lectures on Phase Transitions and the Renormalization Group
    ids: doi:10.1201/9780429493492
    · formatting nit in 'pages'='185-–197' — non-standard page separator in '185-–197'; use '--' [crossref]
    ✓ exactly one match (high) — Matched a single work via crossref.
...

NO ISSUES (18) — verified, nothing to fix:
...
```

### Options

| Option | Description |
| --- | --- |
| `-f, --format text\|json\|both` | Output format. `json` emits the full structured report (verdicts, candidates, features) — ideal for tooling and AI agents. Default: `text`. |
| `--no-network` | Parse only: identifiers + cited/uncited bookkeeping, no database or LLM calls. |
| `--no-llm` | Formal-only: skip LLM adjudication for fully deterministic output (useful in CI). |
| `--fresh` | Ignore cached results and re-query everything. |
| `--cache PATH` | Cache DB location. Default: `<bib_dir>/.reference_audit/cache.db`. |
| `--model NAME` | Override the LLM model (default `gpt-5.4-mini`). |
| `--fail-on hallucinated\|multiple` | Exit non-zero if any entry gets that verdict — for gating submissions in CI. |

Example — fail a CI check if any reference looks hallucinated, as JSON:

```bash
uv run reference-audit audit paper.tex refs.bib --format json --fail-on hallucinated
```

### Reading the output

The text report leads with two headline categories:

- **`CAPITAL OFFENCES`** — hallucinated citations: entries that conclusively resolve to **no** real
  document (verdict *no match*). When there are none, the report says so explicitly
  (*No hallucinated citations*).
- **`UNABLE TO VERIFY`** — entries we could **not** conclusively clear of being a hallucination, for
  any reason: a transient network/LLM error, an unfamiliar `.bib` type, a dead link, an
  adjudication left unsettled. This is the catch-all that keeps an inconclusive check from masquerading
  as a clean pass. When it is empty the report states that for every other reference *at least one
  matching artifact was positively identified*.

The remaining entries (at least one match found) are then split into `ISSUES`, `FORMATTING NITS`, and
`NO ISSUES`. Per-entry verdict glyphs:

- **`✓ exactly one match`** — resolved to a single document; the matched identifier and version
  count are shown.
- **`✗ no match`** — nothing real corresponds; treat as a likely hallucination.
- **`? multiple matches`** — ambiguous; the entry matches more than one distinct work.
- **`unresolved`** — the tool could not conclude (e.g. a transient API error, or a cited web page
  that is a dead link or could not be confirmed). Never reported as a hallucination.
- **`⚠` lines** — per-entry issues: a normalized/backfilled identifier, a missing ISBN, a dangling
  citation, etc.
- The header also lists **cited-but-missing** citations (a `\cite` with no `.bib` entry) and
  **uncited** entries.

## Development

```bash
uv run pytest          # unit + integration tests (databases & LLM are mocked; no network)
uv run cfs validate    # validate the governance artifacts and code traceability
```

Tests mock all database and LLM calls, so the suite is fast and offline. When the tool gets a case
wrong, the fix is captured as a new test with the recorded response (see
[`architecture/SPEC.md`](architecture/SPEC.md), "General Design Principles").

## Constructor Fabric

This project is built and governed with [Constructor Fabric's `studio`](https://github.com/constructorfabric/studio)
(the `cfs` CLI, distributed as the `constructor-studio` package). Studio is a **governance and
traceability framework for AI-assisted delivery** — it is *not* a runtime library, and the audit
pipeline does not call it. The auditor itself is plain async Python; studio governs *how* that code
is specified, traced, and validated.

Concretely:

- **Governed artifacts** live in [`architecture/`](architecture/) — the specification and design
  (SPEC, PRD, DESIGN, DECOMPOSITION) plus per-feature documents
  ([`features/parsing.md`](architecture/features/parsing.md),
  [`features/identification.md`](architecture/features/identification.md)). These are the source of
  truth for what the system is meant to do.
- **Code traceability** links implementation back to those artifacts via `@cpt-*` markers in
  `src/`, so each governed requirement maps to the code that fulfills it. Both the offline parse
  slice and the networked identification/verdict pipeline are traced to code.
- **The validation gate** ties it together:

  ```bash
  uv run cfs validate    # validate the governance artifacts and code traceability
  ```

- **Studio's configuration** lives in `.cf-studio/` (the installed SDLC kit defines the required
  artifact structure), and the root `AGENTS.md` / `CLAUDE.md` files are generated by studio.

`constructor-studio` is pinned as a git dependency in [`pyproject.toml`](pyproject.toml) so the
`cfs` CLI is available after `uv sync`.

### Using Constructor Fabric

You drive studio two ways: directly through the `cfs` CLI, and through your AI assistant, which
follows studio's governed SDLC workflows.

**1. The `cfs` CLI (run these yourself).** After `uv sync` the CLI is on `uv run cfs`:

```bash
uv run cfs validate          # the gate: artifact structure + code traceability must pass
uv run cfs doctor            # environment health check
uv run cfs info              # show project configuration and resolved kit paths
uv run cfs spec-coverage     # report @cpt marker coverage across the codebase
uv run cfs list-ids          # list every cpt-* ID and where it is defined
uv run cfs where-defined cpt-referenceaudit-fr-identify-artifact   # jump to a definition
uv run cfs where-used    cpt-referenceaudit-fr-identify-artifact   # find every reference
uv run cfs map --out map.html   # interactive markdown↔source traceability graph
uv run cfs check-updates     # check studio + installed kits for updates
```

`uv run cfs --help` lists the full command set. **`uv run cfs validate` is the one to run before
every commit** — it is the contract that the `architecture/` artifacts and the `@cpt-*` markers in
`src/` agree.

**2. AI-assisted SDLC workflows.** Studio ships the `sdlc` kit (under `.cf-studio/`) with governed
workflows — `doc-prd`, `doc-design`, `decompose`, `doc-feature`, `implement`, `pr-review`,
`reverse-engineer`, and more. Generate the integration for your editor/agent once:

```bash
uv run cfs generate-agents   # writes agent/command/rule files for Claude, Cursor, Copilot, …
```

Then ask your assistant for the task in natural language and it follows the matching workflow — for
example *"spec a new FEATURE for the canonical .bib output"* runs the `doc-feature` authoring loop
(template → deterministic gate → semantic review), and *"implement that feature"* runs `implement`,
adding `@cpt` markers and syncing the DECOMPOSITION checkboxes. The root `AGENTS.md` / `CLAUDE.md`
(generated by studio) bind these rules into the assistant's context.

**The everyday loop** when you change behavior — manually or via the assistant — is always: update
the governing artifact (PRD/DESIGN/DECOMPOSITION/feature) → update the code and its `@cpt` markers →
run `uv run cfs validate` until green → update `README.md`. A `flow`/`algo`/`dod`/`state` definition
checked `[x]` in a FEATURE **requires** a matching code marker; unchecked **forbids** one. If a
capability is implemented but not yet `@cpt`-traced, leave its box unchecked and say so, rather than
claiming coverage you don't have.

**Updating studio itself:**

```bash
uv run cfs update            # update studio (kits are left alone unless you pass --with-kits yes)
```

## Project layout

```
src/reference_audit/
  parsing/     # .bib / .tex / identifier parsing
  sources/     # modular adapters: Crossref, OpenAlex, Semantic Scholar, arXiv, Open Library,
               #   publisher (DOI landing-page citation export), web (cited-page fetch); + routing
  matching/    # candidate pooling, feature scoring, SAME-OBJECT clustering, verdicts, web check
  llm/         # OpenAI structured-output adjudication (pydantic schemas)
  cache/       # SQLite memoization of DB/LLM calls (errors never cached)
  bookcheck.py # Open Library edition resolution (cited vs. latest edition)
  fieldcheck.py# per-field correctness / formatting findings for an exactly-one match
  versioning.py# better-version detection (published > preprint)
  pipeline.py  # async orchestration
  report.py    # text / JSON rendering
  config.py    # AuditConfig (model, keys, thresholds)
  models.py    # pydantic domain models
  cli.py       # command-line entry point (Typer)
architecture/  # governed specification & design (SPEC, PRD, DESIGN, DECOMPOSITION, features)
tests/
  documents/   # test papers: <paper-title-slug>/<version>.{tex,bib} (initial, polished, …)
  *.py         # mocked unit/integration tests; the pilot paper is the development oracle
```

See [`architecture/SPEC.md`](architecture/SPEC.md) for the full specification and the
`architecture/` artifacts for the detailed design.
