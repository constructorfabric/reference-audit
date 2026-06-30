# Technical Design — Reference Audit


<!-- toc -->

- [1. Architecture Overview](#1-architecture-overview)
  - [1.1 Architectural Vision](#11-architectural-vision)
  - [1.2 Architecture Drivers](#12-architecture-drivers)
  - [1.3 Architecture Layers](#13-architecture-layers)
- [2. Principles & Constraints](#2-principles--constraints)
  - [2.1 Design Principles](#21-design-principles)
  - [2.2 Constraints](#22-constraints)
- [3. Technical Architecture](#3-technical-architecture)
  - [3.1 Domain Model](#31-domain-model)
  - [3.2 Component Model](#32-component-model)
  - [3.3 API Contracts](#33-api-contracts)
  - [3.4 Internal Dependencies](#34-internal-dependencies)
  - [3.5 External Dependencies](#35-external-dependencies)
  - [3.6 Interactions & Sequences](#36-interactions--sequences)
  - [3.7 Database schemas & tables](#37-database-schemas--tables)
- [4. Additional context](#4-additional-context)
- [5. Traceability](#5-traceability)

<!-- /toc -->

- [ ] `p3` - **ID**: `cpt-referenceaudit-design-overview`
## 1. Architecture Overview

### 1.1 Architectural Vision

Reference Audit is a Python library plus CLI organized as a staged pipeline over a small set of
pydantic domain models. The offline parse path is synchronous: `build_parse_report` orchestrates
`.bib` parsing, `.tex` citation extraction, identifier normalization, and deterministic issue
collection into an `AuditReport`.

On top of that foundation, the networked pipeline (`AuditPipeline.run` / `run_audit`) is implemented
as concurrent async per-entry processing: modular database adapters (sources), feature scoring and a
SAME-OBJECT disambiguation rule (matching), an LLM adjudication funnel (llm), URL-only web and Open
Library book verification, and SQLite memoization (cache). The architecture isolates each concern
behind a package boundary so the offline parse slice stays dependency-light and the network/LLM
stages stay swappable. Each entry is audited in isolation: a failure on one reference leaves it
`unresolved` (retried next run) and never aborts the others.

### 1.2 Architecture Drivers

Requirements that significantly influence architecture decisions.

#### Functional Drivers

| Requirement | Design Response |
|-------------|------------------|
| `cpt-referenceaudit-fr-parse-bib-tex` | The `parsing` package + `build_parse_report` orchestration produce a structured `AuditReport` offline. |
| `cpt-referenceaudit-fr-identify-artifact` | The `sources` package (modular DB adapters) returns candidate `SourceRecord`s, preferring DOI/ISBN/URL. |
| `cpt-referenceaudit-fr-three-way-verdict` | The `matching` package collapses candidates into a `none` / `exactly_one` / `multiple` verdict. |
| `cpt-referenceaudit-fr-hallucination-screen` | `matching` + `llm` adjudication drive empty candidate sets to a confident `none`. |
| `cpt-referenceaudit-fr-best-version-canonical` | `matching` ranks versions (published > preprint, later editions) and `report` emits the canonical reference. |

#### NFR Allocation

| NFR ID | NFR Summary | Allocated To | Design Response | Verification Approach |
|--------|-------------|--------------|-----------------|----------------------|
| `cpt-referenceaudit-nfr-offline-deterministic` | Parse slice is offline + deterministic | `parsing`, `pipeline` | No network imports in the parse path; pure functions over file inputs. | Unit tests run with no network. |
| `cpt-referenceaudit-nfr-cached-calls` | Memoize DB/LLM calls | `cache` | SQLite-backed memoization wrapping source/LLM calls. | Integration test asserts cache hits on repeat. |

### 1.3 Architecture Layers

```text
CLI / report  ->  pipeline (orchestration)  ->  parsing | sources | matching | llm
                                              \->  models (shared)   \-> cache
```

- [ ] `p3` - **ID**: `cpt-referenceaudit-tech-python`

| Layer | Responsibility | Technology |
|-------|---------------|------------|
| Presentation | CLI entry point and report rendering | `typer` CLI, `report.py` |
| Application | Pipeline orchestration (parse → route → query → score → adjudicate → cluster → verdict → enrich) | `pipeline.py` (async) |
| Domain | Bib/citation/source/feature models | `pydantic` models |
| Infrastructure | DB/web adapters, LLM client, SQLite cache | `sources`, `llm`, `cache` |

## 2. Principles & Constraints

### 2.1 Design Principles

#### Offline-first parse slice

- [x] `p1` - **ID**: `cpt-referenceaudit-principle-offline-first`

The parse path must perform no network I/O and must be deterministic, so it can run in air-gapped CI
and forms a stable foundation for the networked stages.

#### Modular, swappable sources

- [x] `p2` - **ID**: `cpt-referenceaudit-principle-modular-sources`

Each bibliographic database is a self-contained adapter behind a common interface (`sources/base.py`),
so sources can be added, removed, or reordered without touching matching or pipeline code.

### 2.2 Constraints

#### Identifier preference order

- [x] `p1` - **ID**: `cpt-referenceaudit-constraint-id-preference`

Identification must prefer DOI for papers, ISBN for books, and URL for other artifacts; metadata
matching always runs alongside (to backfill missing identifiers and corroborate), but a strong
identifier match takes precedence.

#### No-network parse path

- [x] `p1` - **ID**: `cpt-referenceaudit-constraint-no-network-parse`

The `parsing` package and `build_parse_report` must not import or invoke any networking code; all
network access is confined to the `sources` and `llm` packages.

## 3. Technical Architecture

### 3.1 Domain Model

**Technology**: pydantic models

**Location**: [models.py](../src/reference_audit/models.py)

**Core Entities**:

| Entity | Description | Schema |
|--------|-------------|--------|
| BibEntry | A parsed `.bib` entry with normalized `Identifiers`. | [models.py](../src/reference_audit/models.py) |
| Identifiers | Normalized DOI / ISBN13 / arXiv / OpenAlex Work id / Google Books volume id / URL / PMID. | [models.py](../src/reference_audit/models.py) |
| EntryAudit | A `BibEntry` plus its verdict and issue list. | [models.py](../src/reference_audit/models.py) |
| AuditReport | The aggregate report (entries + bookkeeping + summary). | [models.py](../src/reference_audit/models.py) |
| SourceRecord | A candidate artifact returned by a database/web adapter. | [models.py](../src/reference_audit/models.py) |
| Verdict / MatchedArtifact | The 3-way verdict and the clustered artifact(s) it resolves to. | [models.py](../src/reference_audit/models.py) |
| FieldFinding | A per-field correctness/formatting finding for an exactly-one match. | [models.py](../src/reference_audit/models.py) |

**Relationships**:
- AuditReport → EntryAudit: contains one audit per parsed entry.
- EntryAudit → BibEntry → Identifiers: each audit wraps one entry, which owns its identifiers.

### 3.2 Component Model

```text
parsing -> models <- pipeline -> sources -> cache
                         |-> matching -> llm
                         |-> report / cli
```

> **Checkbox semantics:** `[x]` here means *implemented **and** traced to code* via `@cpt` markers.
> Today only `parsing`, `models`, and `report-cli` are `@cpt`-traced. The `sources`, `matching`,
> `llm`, and `cache` components are fully **implemented in code** but are not yet `@cpt`-traced, so
> they remain unchecked even though their scope notes below read *IMPLEMENTED*. Adding that
> traceability (and a `features/identification.md` spec) is the outstanding governance work.

#### parsing

- [x] `p1` - **ID**: `cpt-referenceaudit-component-parsing`

##### Why this component exists

To turn raw `.bib` and `.tex` text into clean structured data (entries, cited keys, normalized
identifiers) so every downstream stage works against models instead of LaTeX/BibTeX syntax.

##### Responsibility scope

Parse `.bib` into `BibEntry`s (`parsing/bib.py`), extract cited keys and resolve includes from
`.tex` (`parsing/tex.py`), and normalize DOI/ISBN/arXiv/OpenAlex-Work-id/Google-Books-volume-id
identifiers (`parsing/identifiers.py`).
Detect commented preprint twins. **IMPLEMENTED (M1).**

##### Responsibility boundaries

Does no network I/O, no database lookups, and no verdict computation; it only produces normalized
structured data.

##### Related components (by ID)

- `cpt-referenceaudit-component-models` — depends on (produces `BibEntry` / `Identifiers`)

#### models

- [x] `p1` - **ID**: `cpt-referenceaudit-component-models`

##### Why this component exists

To provide a single shared, validated domain vocabulary used by every other component.

##### Responsibility scope

Define pydantic models (`BibEntry`, `Identifiers`, `EntryAudit`, `AuditReport`, `SourceRecord`,
`FeatureVector`, `EntryType`) and the bib-type mapping. **IMPLEMENTED.**

##### Responsibility boundaries

Holds no behavior beyond validation and small derived helpers; performs no I/O.

##### Related components (by ID)

- `cpt-referenceaudit-component-parsing` — shares model with

#### sources

- [x] `p2` - **ID**: `cpt-referenceaudit-component-sources`

##### Why this component exists

To query external bibliographic databases and pages and return candidate artifacts for identification.

##### Responsibility scope

Modular adapters (Crossref, OpenAlex, Semantic Scholar, arXiv, DBLP, Open Library, Google Books, the
publisher DOI landing-page citation export, and a web page fetcher) producing `SourceRecord`s behind
a common interface, with per-entry routing by id vs. metadata. The web page fetcher additionally
detects JavaScript single-page-app shells (a served page with no readable content) and re-fetches
them through a headless browser (`render`) so the rendered page can be read; when no browser is
available the page is marked unrenderable rather than read as empty. **IMPLEMENTED.**

##### Responsibility boundaries

Performs no scoring or verdict logic; returns raw candidates only. The publisher adapter is advisory
only (never an identity source), so a bot-walled publisher cannot mask a hallucinated DOI.

##### Related components (by ID)

- `cpt-referenceaudit-component-cache` — depends on (memoizes queries)
- `cpt-referenceaudit-component-matching` — publishes to (provides candidates)

#### matching

- [x] `p1` - **ID**: `cpt-referenceaudit-component-matching`

##### Why this component exists

To decide, from candidate records, whether a reference matches no artifact, exactly one, or multiple
— the heart of the audit — and to select the best version.

##### Responsibility scope

Candidate pooling, feature scoring (`FeatureVector`), the SAME-OBJECT clustering rule (`sameobject`),
the 3-way verdict (`verdict`), URL-only web verification (`webcheck` — including the rule that a
JavaScript app shell that could not be rendered is left unresolved, never read as a wrong/`none`
URL), and version ranking. **IMPLEMENTED.**

##### Responsibility boundaries

Does not call databases directly (consumes candidates from `sources`) and does not render output.

##### Related components (by ID)

- `cpt-referenceaudit-component-sources` — subscribes to (consumes candidates)
- `cpt-referenceaudit-component-llm` — calls (adjudication funnel)

#### llm

- [x] `p2` - **ID**: `cpt-referenceaudit-component-llm`

##### Why this component exists

To adjudicate ambiguous matches that feature scoring cannot resolve on its own.

##### Responsibility scope

OpenAI structured-output (pydantic-schema) adjudication invoked by `matching` for hard cases:
per-candidate "can this record correspond to the entry?", the SAME-OBJECT tie-break, web-page
confirmation, and per-field correctness. Decisions are cached by `(prompt, kind, model)`.
**IMPLEMENTED.**

##### Responsibility boundaries

Stateless with respect to the audit; returns structured judgments, never final report formatting.

##### Related components (by ID)

- `cpt-referenceaudit-component-cache` — depends on (memoizes LLM calls)

#### cache

- [x] `p2` - **ID**: `cpt-referenceaudit-component-cache`

##### Why this component exists

To bound cost and latency by memoizing slow, metered database and LLM calls.

##### Responsibility scope

SQLite-backed memoization of source queries, LLM decisions, whole-entry verdicts, and DOI
resolutions, gated by `pipeline_version`/`model`. Only successful results are stored — errors are
never cached, so an outage retries rather than being recorded as a miss. **IMPLEMENTED.**

##### Responsibility boundaries

Stores and retrieves responses only; contains no audit logic.

##### Related components (by ID)

- `cpt-referenceaudit-component-sources` — owns data for (cached query results)

#### report-cli

- [x] `p1` - **ID**: `cpt-referenceaudit-component-report-cli`

##### Why this component exists

To present the `AuditReport` to humans and machines and to provide the program entry point.

##### Responsibility scope

`report.py` renders JSON/text (verdict-aware categories: capital offences, unable-to-verify, issues,
nits, clean); `cli.py` (Typer) parses arguments and runs either the parse-only or the full audit.
**IMPLEMENTED.**

##### Responsibility boundaries

Contains no parsing, matching, or network logic; only formats results and wires the entry point.

##### Related components (by ID)

- `cpt-referenceaudit-component-parsing` — calls (via pipeline orchestration)

### 3.3 API Contracts

The public surface is the `build_parse_report` library function and the `reference-audit` CLI.

This realizes the PRD public interface `cpt-referenceaudit-interface-parse-report`.

- [ ] `p2` - **ID**: `cpt-referenceaudit-interface-cli`

- **Implements (PRD)**: `cpt-referenceaudit-interface-parse-report`
- **Contracts**: `cpt-referenceaudit-contract-database-query`
- **Technology**: Python function call + CLI (Typer)
- **Location**: [pipeline.py](../src/reference_audit/pipeline.py), [cli.py](../src/reference_audit/cli.py)

**Endpoints Overview**:

| Method | Path | Description | Stability |
|--------|------|-------------|-----------|
| `CALL` | `build_parse_report(tex_path, bib_path)` | Parse-only audit returning an `AuditReport`. | unstable |
| `CALL` | `run_audit(tex_path, bib_path, ...)` | Full networked audit returning an `AuditReport` with verdicts. | unstable |
| `CLI` | `reference-audit audit` | Run the audit from the command line (`--no-network`, `--no-llm`, `--fresh`, `--fail-on`, ...). | unstable |

### 3.4 Internal Dependencies

| Dependency Module | Interface Used | Purpose |
|-------------------|----------------|----------|
| models | pydantic models | Shared domain vocabulary for all components |
| parsing | `parse_bib`, `parse_cited_keys` | Produce entries + cited keys for the pipeline |

**Dependency Rules** (per project conventions):
- No circular dependencies.
- The parse path imports only `models` and `parsing`.

### 3.5 External Dependencies

External libraries and services this module interacts with.

#### Bibliographic databases & parsing libraries

| Dependency Module | Interface Used | Purpose |
|-------------------|---------------|---------|
| bibtexparser | `loads` / `db.entries` | Parse `.bib` source |
| pydantic / pydantic-settings | `BaseModel` / `BaseSettings` | Validated domain models + config |
| httpx / curl-cffi | async HTTP clients | Source queries; bot-walled publisher fetch |
| beautifulsoup4 | HTML parsing | Web/publisher page metadata extraction |
| openai | structured-output chat | LLM adjudication |
| rapidfuzz / anyascii | fuzzy string / transliteration | Title/author similarity features |
| Crossref / OpenAlex / S2 / arXiv / DBLP / Open Library / Google Books | HTTPS JSON APIs | Candidate identification |

**Dependency Rules** (per project conventions):
- Only the `sources` and `llm` components talk to external network services.
- The parse path has no external network dependencies.

### 3.6 Interactions & Sequences

#### Build parse report

- [x] `p1` - **ID**: `cpt-referenceaudit-seq-build-parse-report`

**Use cases**: `cpt-referenceaudit-usecase-parse-audit`

**Actors**: `cpt-referenceaudit-actor-author`

```mermaid
sequenceDiagram
    participant A as Author
    participant P as pipeline.build_parse_report
    participant B as parsing.bib
    participant T as parsing.tex
    A->>P: build_parse_report(tex, bib)
    P->>B: parse_bib(bib)
    B-->>P: entries, twins
    P->>T: parse_cited_keys(tex)
    T-->>P: cited keys, missing includes
    P->>P: mark cited, collect issues, assemble report
    P-->>A: AuditReport
```

**Description**: The implemented offline path that produces an `AuditReport` from `.bib` + `.tex`.

#### Identify and adjudicate

- [x] `p2` - **ID**: `cpt-referenceaudit-seq-identify-adjudicate`

**Use cases**: `cpt-referenceaudit-usecase-parse-audit`

**Actors**: `cpt-referenceaudit-actor-database`

```mermaid
sequenceDiagram
    participant P as pipeline
    participant C as cache
    participant S as sources
    participant M as matching
    participant L as llm
    P->>C: get cached verdict?
    C-->>P: miss
    P->>S: query(ids + metadata, concurrent)
    S-->>M: candidate SourceRecords (pooled)
    M->>M: score + bucket
    M->>L: adjudicate non-clean cases (per-candidate, SAME-OBJECT, web, fields)
    L-->>M: structured judgment (cached)
    M-->>P: 3-way verdict + canonical best version
    P->>C: store successful verdict
```

**Description**: The networked path layered on top of the parse slice. A clean formal `exactly_one`
short-circuits the LLM; books are confirmed against Open Library and URL-only `@misc` against their
own page. Each entry is isolated, and only successful results are cached.

### 3.7 Database schemas & tables

The offline parse path uses no persistent database. The networked path memoizes calls in a local
SQLite cache (`cache/db.py`, `cache/store.py`) with four cache tables plus a `db_quirks` log. Only
successful results are stored; errors are never cached, so an outage retries.

- [x] `p3` - **ID**: `cpt-referenceaudit-db-cache`

#### Tables: SQLite response cache

**ID**: `cpt-referenceaudit-dbtable-response-cache`

**Schema**:

| Table | Primary key | Stores |
|-------|-------------|--------|
| `source_query_cache` | `(entry_hash, source, query_kind)` | Raw adapter responses (id / metadata / editions / web). |
| `llm_decision_cache` | `(prompt_hash, kind, model)` | LLM judgments — model in the key, so a model switch re-runs. |
| `entry_verdict_cache` | `entry_hash` | Whole-entry verdict fast path, gated by `pipeline_version` + `model`. |
| `doi_resolution_cache` | `doi` | doi.org's verdict on a DOI (a world-fact; model/version-independent). |
| `db_quirks` | — | Notes on database quirks encountered (design principle 2). |

**Constraints**: Only successful (`ok=1`) source results and definitive DOI resolutions are stored;
transient errors are never cached, preserving the error ≠ not-found invariant.

**Example** (`source_query_cache`):

| entry_hash | source | query_kind | ok | fetched_at |
|------------|--------|------------|----|------------|
| 9f2a... | crossref | id | 1 | 2026-06-27T00:00:00Z |

## 4. Additional context

- PRD: [PRD.md](./PRD.md)
- Decomposition: [DECOMPOSITION.md](./DECOMPOSITION.md)

## 5. Traceability

- **PRD**: [PRD.md](./PRD.md)
- **ADRs**: [ADR/](./ADR/)
- **Features**: [features/](./features/)
