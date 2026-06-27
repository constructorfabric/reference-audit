# PRD — Reference Audit

<!-- toc -->

- [1. Overview](#1-overview)
  - [1.1 Purpose](#11-purpose)
  - [1.2 Background / Problem Statement](#12-background--problem-statement)
  - [1.3 Goals (Business Outcomes)](#13-goals-business-outcomes)
  - [1.4 Glossary](#14-glossary)
- [2. Actors](#2-actors)
  - [2.1 Human Actors](#21-human-actors)
  - [2.2 System Actors](#22-system-actors)
- [3. Operational Concept & Environment](#3-operational-concept--environment)
  - [3.1 Module-Specific Environment Constraints](#31-module-specific-environment-constraints)
- [4. Scope](#4-scope)
  - [4.1 In Scope](#41-in-scope)
  - [4.2 Out of Scope](#42-out-of-scope)
- [5. Functional Requirements](#5-functional-requirements)
  - [5.1 Parsing & Bookkeeping (M1, implemented)](#51-parsing--bookkeeping-m1-implemented)
  - [5.2 Identification & Verdict (M2–M5, planned)](#52-identification--verdict-m2m5-planned)
- [6. Non-Functional Requirements](#6-non-functional-requirements)
  - [6.1 NFR Inclusions](#61-nfr-inclusions)
  - [6.2 NFR Exclusions](#62-nfr-exclusions)
- [7. Public Library Interfaces](#7-public-library-interfaces)
  - [7.1 Public API Surface](#71-public-api-surface)
  - [7.2 External Integration Contracts](#72-external-integration-contracts)
- [8. Use Cases](#8-use-cases)
- [9. Acceptance Criteria](#9-acceptance-criteria)
- [10. Dependencies](#10-dependencies)
- [11. Assumptions](#11-assumptions)
- [12. Risks](#12-risks)

<!-- /toc -->

## 1. Overview

### 1.1 Purpose

Reference Audit is a library and CLI that audits a paper's bibliography (`.bib`) against its
manuscript (`.tex`). For each reference it (1) identifies the exact artifact the reference points
to and returns a 3-way verdict — no real match (hallucinated) / exactly one / multiple — preferring
DOI for papers, ISBN for books, and URL for other artifacts; (2) finds a better version of the same
work (published over preprint, later editions); and (3) emits the canonical best reference.

It is used by authors checking their own bibliographies, by automated hallucination-screening
pipelines, and by AI agents writing papers that need to verify the references they cite are real.

### 1.2 Background / Problem Statement

LLM-assisted writing has made fabricated ("hallucinated") references a routine failure mode:
plausible-looking entries with invented DOIs, wrong years, or nonexistent venues slip into
bibliographies. Manual verification is slow and error-prone, and existing linters only catch
formatting issues, not whether the cited work actually exists.

At the same time, even genuine references are frequently sub-optimal: a preprint is cited when a
peer-reviewed version exists, or an older edition is cited when a newer one supersedes it. Authors
and downstream tools need a single component that both screens for hallucinations and upgrades each
reference to its canonical best form, working from the same `.bib` + `.tex` inputs that already
exist in every LaTeX project.

The first milestone (M1) ships a fully offline, no-network parse-only slice: parse the `.bib`,
extract `.tex` citations, normalize identifiers, and report bookkeeping (cited/uncited/missing) plus
deterministic metadata issues. Identification, verdicts, and best-version selection are later
milestones that build on this foundation.

### 1.3 Goals (Business Outcomes)

- Reliably flag fabricated references so that hallucinated entries are caught before submission.
- Reduce the manual effort of bibliography verification to a single command or library call.
- Improve citation quality by emitting the canonical best version of each genuine reference.

### 1.4 Glossary

| Term | Definition |
|------|------------|
| reference | A single bibliography entry (`@article`, `@book`, ...) in the `.bib` file. |
| artifact | The real-world scholarly object (paper, book, dataset) a reference points to. |
| verdict | The 3-way identification result: `none` / `exactly_one` / `multiple`. |
| hallucination | A reference with no real matching artifact in any source. |
| canonical reference | The single best version of a work (published over preprint, latest edition). |
| cited / uncited | Whether a `.bib` key appears in a `\cite`-family command in the `.tex`. |
| commented twin | A `%`-commented `.bib` entry that is the preprint twin of a live entry; informational. |

## 2. Actors

> **Note**: Stakeholder needs are managed at project/task level by steering committee. Document **actors** (users, systems) that interact with this module.

### 2.1 Human Actors

#### Author

**ID**: `cpt-referenceaudit-actor-author`

**Role**: A researcher auditing their own `.bib` + `.tex` before submission.
**Needs**: A fast verdict on which references are real, which are uncited, and which can be upgraded.

#### Reviewer / Screener

**ID**: `cpt-referenceaudit-actor-reviewer`

**Role**: A person or team running hallucination screening over submitted manuscripts.
**Needs**: A reliable `none` verdict for invented entries and a machine-readable report.

### 2.2 System Actors

#### AI Writing Agent

**ID**: `cpt-referenceaudit-actor-ai-agent`

**Role**: An automated agent that writes papers and calls Reference Audit to verify the references it has produced are real before committing them.

#### Bibliographic Database

**ID**: `cpt-referenceaudit-actor-database`

**Role**: An external scholarly database (Crossref, OpenAlex, Semantic Scholar, arXiv, OpenLibrary) queried to identify and compare artifacts. (Used by later milestones; not contacted in the parse-only slice.)

## 3. Operational Concept & Environment

> **Note**: Project-wide runtime, OS, architecture, lifecycle policy, and integration patterns defined in root PRD. Document only module-specific deviations here.

### 3.1 Module-Specific Environment Constraints

- The parse-only slice (M1) MUST run with no network access.
- Later milestones require outbound HTTPS access to scholarly databases and (optionally) an OpenAI API key for LLM adjudication.
- Python 3.13+ with `pydantic` and `bibtexparser` as core dependencies.

## 4. Scope

### 4.1 In Scope

- Parsing `.bib` entries into structured records and extracting `.tex` citations.
- Normalizing DOI / ISBN / arXiv identifiers and detecting commented preprint twins.
- Reporting cited / uncited keys, missing `\input`/`\include` files, and citation-vs-bib mismatches.
- Flagging deterministic metadata issues visible from the `.bib` alone.
- (Later) identifying artifacts against databases, the 3-way verdict, hallucination screening, best-version selection, and canonical output.

### 4.2 Out of Scope

- Rewriting or re-typesetting the manuscript body.
- Generating new references or filling in missing citations.
- Validating non-bibliographic LaTeX (math, figures, formatting).

## 5. Functional Requirements

> **Testing strategy**: All requirements verified via automated tests (unit, integration, e2e) targeting 90%+ code coverage unless otherwise specified.

Functional requirements define WHAT the system must do.

### 5.1 Parsing & Bookkeeping (M1, implemented)

#### Parse bib and tex with identifier normalization

- [x] `p1` - **ID**: `cpt-referenceaudit-fr-parse-bib-tex`

The system **MUST** parse `.bib` entries and `.tex` citations, normalize DOI/ISBN/arXiv identifiers,
report cited / uncited keys and missing `\input`/`\include` files, and flag deterministic metadata
issues — all with no network access.

**Rationale**: A correct structured view of the bibliography and citations is the foundation every later audit step builds on.

**Actors**: `cpt-referenceaudit-actor-author`

### 5.2 Identification & Verdict (M2–M5, planned)

#### Identify the artifact behind a reference

- [ ] `p1` - **ID**: `cpt-referenceaudit-fr-identify-artifact`

The system **MUST** query bibliographic databases and return the artifact(s) matching a reference,
preferring DOI (papers), ISBN (books), and URL (other artifacts).

**Rationale**: Identification is the core capability that distinguishes real references from fabricated ones.

**Actors**: `cpt-referenceaudit-actor-author`, `cpt-referenceaudit-actor-database`

#### Three-way identification verdict

- [ ] `p1` - **ID**: `cpt-referenceaudit-fr-three-way-verdict`

The system **MUST** return one of three verdicts per reference: `none`, `exactly_one`, or `multiple`.

**Rationale**: A discrete verdict lets authors and tools act deterministically on each reference.

**Actors**: `cpt-referenceaudit-actor-author`

#### Hallucination screening

- [ ] `p1` - **ID**: `cpt-referenceaudit-fr-hallucination-screen`

The system **MUST** reliably return `none` for invented / fabricated entries.

**Rationale**: Catching hallucinated references before submission is the primary screening use case.

**Actors**: `cpt-referenceaudit-actor-reviewer`, `cpt-referenceaudit-actor-ai-agent`

#### Best version and canonical output

- [ ] `p2` - **ID**: `cpt-referenceaudit-fr-best-version-canonical`

The system **MUST** select a better version of a matched work (published over preprint, later
editions) and emit the canonical best reference.

**Rationale**: Upgrading references to their canonical form improves citation quality and reproducibility.

**Actors**: `cpt-referenceaudit-actor-author`

## 6. Non-Functional Requirements

### 6.1 NFR Inclusions

#### Offline determinism of the parse slice

- [x] `p1` - **ID**: `cpt-referenceaudit-nfr-offline-deterministic`

The parse-only path **MUST** be deterministic and perform no network I/O, so results are
reproducible and the slice can run in air-gapped CI.

**Threshold**: Zero network calls in the parse path; identical output for identical inputs.

**Rationale**: Reproducibility and the ability to run in restricted environments are required for screening pipelines.

#### Caching of database and LLM calls

- [ ] `p2` - **ID**: `cpt-referenceaudit-nfr-cached-calls`

Later milestones **SHOULD** memoize database and LLM responses to bound cost and latency on repeated audits.

**Threshold**: Repeated audits of the same inputs reuse cached responses rather than re-querying.

**Rationale**: Database and LLM calls are slow and metered; caching keeps repeated audits cheap.

### 6.2 NFR Exclusions

- (none)

## 7. Public Library Interfaces

Define the public API surface and integration contracts provided by this library.

### 7.1 Public API Surface

#### Parse report API

- [x] `p1` - **ID**: `cpt-referenceaudit-interface-parse-report`

**Type**: Python function (`reference_audit.pipeline.build_parse_report`) returning an `AuditReport`.

**Stability**: unstable

**Description**: Given a `.tex` path (optional) and a `.bib` path, returns a structured `AuditReport` with per-entry identifiers, issues, and bookkeeping.

**Breaking Change Policy**: Pre-1.0; signatures may change between milestones.

### 7.2 External Integration Contracts

Contracts this library expects from external systems.

#### Scholarly database query contract

- [ ] `p2` - **ID**: `cpt-referenceaudit-contract-database-query`

**Direction**: required from client (database adapters)

**Protocol/Format**: HTTPS / JSON over each provider's public API.

**Compatibility**: Provider-specific; isolated behind source adapters (planned, M2–M3).

## 8. Use Cases

#### Audit a bib + tex parse-only

- [x] `p1` - **ID**: `cpt-referenceaudit-usecase-parse-audit`

**Actor**: `cpt-referenceaudit-actor-author`

**Preconditions**:
- A `.bib` file exists; a `.tex` file optionally exists.

**Main Flow**:
1. The author runs the parse-only audit on their `.bib` (and optional `.tex`).
2. The system parses entries, normalizes identifiers, and extracts citations.
3. The system reports cited/uncited keys, missing includes, commented twins, and metadata issues.

**Postconditions**:
- The author has a structured report of bibliography bookkeeping and metadata issues.

**Alternative Flows**:
- **No `.tex` provided**: Nothing is reported as uncited; only `.bib`-derived issues are produced.

## 9. Acceptance Criteria

- [ ] Parse-only audit returns correct cited/uncited/missing-include counts for a known fixture.
- [ ] DOI, ISBN, and arXiv identifiers are normalized to canonical forms.
- [ ] Commented preprint twins are routed to an informational list, never the audited list.

## 10. Dependencies

| Dependency | Description | Criticality |
|------------|-------------|-------------|
| bibtexparser | `.bib` parsing | p1 |
| pydantic | Data models | p1 |
| Scholarly databases | Identification (later milestones) | p2 |

## 11. Assumptions

- Inputs are well-formed enough for `bibtexparser` to load (malformed entries are tolerated best-effort).
- For later milestones, the relevant databases are reachable and expose the cited works.

## 12. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Database coverage gaps | A real reference may be misjudged `none` | Query multiple sources; reserve LLM adjudication |
| Identifier ambiguity | Wrong artifact matched | Prefer DOI/ISBN/URL; apply same-object disambiguation rule (M5) |
