# Feature: Parsing


<!-- toc -->

- [1. Feature Context](#1-feature-context)
  - [1.1 Overview](#11-overview)
  - [1.2 Purpose](#12-purpose)
  - [1.3 Actors](#13-actors)
  - [1.4 References](#14-references)
- [2. Actor Flows (CDSL)](#2-actor-flows-cdsl)
  - [Build parse report](#build-parse-report)
- [3. Processes / Business Logic (CDSL)](#3-processes--business-logic-cdsl)
  - [Normalize identifiers](#normalize-identifiers)
- [4. States (CDSL)](#4-states-cdsl)
  - [Entry citation status](#entry-citation-status)
- [5. Definitions of Done](#5-definitions-of-done)
  - [Parse-only pipeline produces correct bookkeeping](#parse-only-pipeline-produces-correct-bookkeeping)
  - [Identifier normalization is correct](#identifier-normalization-is-correct)
- [6. Acceptance Criteria](#6-acceptance-criteria)

<!-- /toc -->

- [x] `p1` - **ID**: `cpt-referenceaudit-featstatus-parsing`
## 1. Feature Context

- [x] `p1` - `cpt-referenceaudit-feature-parsing`

### 1.1 Overview

The implemented offline parse-only slice: parse `.bib` entries and `.tex` citations, normalize
identifiers, and assemble the `AuditReport` with cited/uncited bookkeeping and deterministic issues.

### 1.2 Purpose

This feature realizes the parse-only foundation of the audit pipeline with no network access. It is
orchestrated by `pipeline.build_parse_report`, which calls `parsing.bib.parse_bib`,
`parsing.tex.parse_cited_keys`, and `pipeline._parse_issues`.

**Requirements**: `cpt-referenceaudit-fr-parse-bib-tex`, `cpt-referenceaudit-nfr-offline-deterministic`

**Principles**: `cpt-referenceaudit-principle-offline-first`

### 1.3 Actors

| Actor | Role in Feature |
|-------|-----------------|
| `cpt-referenceaudit-actor-author` | Runs the parse-only audit over their `.bib` + `.tex`. |

### 1.4 References

- **PRD**: [PRD.md](../PRD.md)
- **Design**: [DESIGN.md](../DESIGN.md)
- **Dependencies**: None

## 2. Actor Flows (CDSL)

User-facing interaction: an author requests a parse-only audit and receives an `AuditReport`.

**Use cases**: `cpt-referenceaudit-usecase-parse-audit`

### Build parse report

- [x] `p1` - **ID**: `cpt-referenceaudit-flow-parsing-build-report`

**Actor**: `cpt-referenceaudit-actor-author`

**Success Scenarios**:
- A valid `.bib` (and optional `.tex`) yields an `AuditReport` with correct bookkeeping.

**Error Scenarios**:
- Missing `\input`/`\include` targets are reported, not fatal.

**Steps**:
1. [x] - `p1` - Parse the `.bib` into entries and commented twins (`parse_bib`) - `inst-parse-bib`
2. [x] - `p1` - Parse the `.tex` for cited keys and missing includes (`parse_cited_keys`) - `inst-parse-tex`
3. [x] - `p1` - Mark each entry `cited` and compute cited-but-missing / uncited sets - `inst-mark-cited`
4. [x] - `p1` - **FOR EACH** entry collect deterministic issues (`_parse_issues`) - `inst-collect-issues`
5. [x] - `p1` - **RETURN** the assembled `AuditReport` with summary counts - `inst-build-report`

## 3. Processes / Business Logic (CDSL)

### Normalize identifiers

Identifier normalization (`parsing/identifiers.py`: `normalize_doi`, `normalize_isbn13`,
`extract_arxiv_id`, `normalize_openalex_id`, `normalize_url`) is implemented and exercised by the
build-report flow's `inst-parse-bib` step. It is documented here as a process for completeness but is
not yet traced as a standalone instruction-level algorithm; instruction-granular tracing of these
helpers is planned. An `openalex.org/W…` URL is extracted into a first-class `Identifiers.openalex`
Work id (and dropped from `url`, so it is not mistaken for a generic web page); a bare `W…` token
with no `openalex.org` host is not trusted.

**Input**: Raw `.bib` field strings (doi, isbn, eprint, url).

**Output**: A normalized `Identifiers` record attached to each `BibEntry`.

## 4. States (CDSL)

### Entry citation status

Each `BibEntry` carries a boolean `cited` flag set during the build-report flow's `inst-mark-cited`
step. The lifecycle is documented here for completeness; it is not modeled as a standalone, traced
state machine in this slice.

**States**: UNCITED, CITED

**Initial State**: UNCITED

**Transition**: UNCITED → CITED when the entry key appears in a `\cite`-family command or `\nocite{*}`.

## 5. Definitions of Done

### Parse-only pipeline produces correct bookkeeping

- [x] `p1` - **ID**: `cpt-referenceaudit-dod-parsing-bookkeeping`

The system **MUST** assemble an `AuditReport` whose cited / uncited / cited-but-missing /
commented-twins / missing-includes counts are correct for the parsed inputs.

**Implements**:
- `cpt-referenceaudit-flow-parsing-build-report`

**Constraints**: `cpt-referenceaudit-constraint-no-network-parse`

**Touches**:
- API: `build_parse_report(tex_path, bib_path)`
- Entities: `AuditReport`, `EntryAudit`, `BibEntry`

### Identifier normalization is correct

- [x] `p1` - **ID**: `cpt-referenceaudit-dod-parsing-identifiers`

The system **MUST** normalize DOI, ISBN, arXiv, and OpenAlex Work identifiers to their canonical
forms.

**Implements**:
- `cpt-referenceaudit-flow-parsing-build-report`

**Constraints**: `cpt-referenceaudit-constraint-no-network-parse`

**Touches**:
- Entities: `Identifiers`, `BibEntry`

## 6. Acceptance Criteria

- [x] Parse-only audit returns correct cited/uncited/missing-include counts for the pilot fixture.
- [x] DOI/ISBN/arXiv identifiers are normalized to canonical forms.
- [x] Commented preprint twins are routed to an informational list, never the audited list.
