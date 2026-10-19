# Feature: Word Input

Read native Word bibliography sources and citation fields offline, with explicit structured-only
coverage warnings and independent reporting of unreadable records.

<!-- toc -->

- [1. Feature Context](#1-feature-context)
  - [1.1 Overview](#11-overview)
  - [1.2 Purpose](#12-purpose)
  - [1.3 Actors](#13-actors)
  - [1.4 References](#14-references)
- [2. Actor Flows (CDSL)](#2-actor-flows-cdsl)
  - [Audit structured Word sources](#audit-structured-word-sources)
- [3. Processes / Business Logic (CDSL)](#3-processes--business-logic-cdsl)
  - [Read native source records and citation fields](#read-native-source-records-and-citation-fields)
- [4. States (CDSL)](#4-states-cdsl)
  - [Word citation linking](#word-citation-linking)
- [5. Definitions of Done](#5-definitions-of-done)
  - [Structured scope is explicit and records are independent](#structured-scope-is-explicit-and-records-are-independent)
- [6. Acceptance Criteria](#6-acceptance-criteria)

<!-- /toc -->

- [ ] `p1` - **ID**: `cpt-referenceaudit-featstatus-word-input`

> **Status: IMPLEMENTED, not yet instruction-level `@cpt`-traced.** Code: `parsing/docx.py`,
> `inputs.py`, `pipeline.py`, `cli.py`, `models.py`, `report.py`. Tests: `tests/test_docx.py`,
> including the copied Office-edited `tests/fixtures/docx/AI4X-2027.docx` (see its `SOURCES.md`).
> Task checkboxes stay unchecked until instruction-level code markers land.

## 1. Feature Context

- [ ] `p1` - `cpt-referenceaudit-feature-word-input`

### 1.1 Overview

Standalone `.docx` inputs supply authored native Word bibliography sources and `CITATION` fields.
Only the structured bibliography is checked. Free-form references are not checked and may be
missed, even in documents containing native sources. Every CLI run must warn prominently.

### 1.2 Purpose

Supply shared `BibEntry` and `CitationContext` records without Word, GROBID, conversion or an LLM.
Reuse identifier normalization, sentence boundaries, caching and isolated per-entry audits.

**Requirements**: `cpt-referenceaudit-fr-audit-word`, `cpt-referenceaudit-nfr-offline-deterministic`

**Principles**: `cpt-referenceaudit-principle-offline-first`

### 1.3 Actors

| Actor | Role in Feature |
|-------|-----------------|
| `cpt-referenceaudit-actor-author` | Checks structured references authored in Word. |
| `cpt-referenceaudit-actor-ai-agent` | Processes supported Word documents in bulk. |

### 1.4 References

- **PRD**: [PRD.md](../PRD.md)
- **Design**: [DESIGN.md](../DESIGN.md)
- **Dependencies**: `cpt-referenceaudit-feature-parsing`, `cpt-referenceaudit-feature-identification`

## 2. Actor Flows (CDSL)

**Use cases**: `cpt-referenceaudit-usecase-audit-word`

### Audit structured Word sources

- [ ] `p1` - **ID**: `cpt-referenceaudit-flow-word-input-audit`

**Actor**: `cpt-referenceaudit-actor-author`

**Success Scenarios**: A `.docx` supplies all stored native sources and linked citing contexts.

**Error Scenarios**: No structured sources or corrupt package/XML/fields fails explicitly; an
unreadable individual source is logged and reported without stopping its neighbors.

**Steps**:
1. [ ] - `p1` - Accept `.docx` as the whole input and reject any second input - `inst-dispatch`
2. [ ] - `p1` - Print the structured-only coverage warning to stderr before reading - `inst-warn`
3. [ ] - `p1` - Read the source records and citation fields offline - `inst-read`
4. [ ] - `p1` - Build the report including scope notes and unparsed sources - `inst-report`
5. [ ] - `p1` - If networking is enabled, reuse isolated per-entry identification and alignment - `inst-audit`
6. [ ] - `p1` - Return text/JSON with Word provenance and cache beside the input - `inst-output`

## 3. Processes / Business Logic (CDSL)

### Read native source records and citation fields

- [ ] `p1` - **ID**: `cpt-referenceaudit-algo-word-input-read`

**Input**: A local `.docx` ZIP package with native bibliography XML.

**Output**: Entries, unparsed sources, cited Tags, contexts, and native field count.

**Steps**:
1. [ ] - `p1` - Validate package parts and XML; reject DTDs and parts above 32 MiB - `inst-package`
2. [ ] - `p1` - Read Sources/Source from custom XML parts and detect duplicate/missing Tags - `inst-sources`
3. [ ] - `p1` - Map each source through shared entry construction or log/report its parse failure - `inst-map`
4. [ ] - `p1` - Walk simple/complex fields across runs and paragraphs in document stories - `inst-fields`
5. [ ] - `p1` - Recover CITATION Tags including grouped tags, suppress bibliography results/deletions - `inst-tags`
6. [ ] - `p1` - Use shared sentence spans for stable per-key citing contexts - `inst-contexts`
7. [ ] - `p1` - Return every stored source outcome; never infer entries from free-form prose - `inst-return`

## 4. States (CDSL)

### Word citation linking

- [ ] `p1` - **ID**: `cpt-referenceaudit-state-word-input-linking`

Native citation fields present → linking available; cited/uncited/missing bookkeeping uses Tags.
No native fields → linking unavailable; citedness unknown and uncited reporting suppressed.
A cited Tag with an unparsed source is reported as unparsed rather than missing.

## 5. Definitions of Done

### Structured scope is explicit and records are independent

- [ ] `p1` - **ID**: `cpt-referenceaudit-dod-word-input-supported`

Every CLI Word run MUST print a prominent warning that only the structured bibliography is
processed and free-form references are not checked and may be missed in mixed documents.
The report MUST preserve that warning in notes and JSON stdout MUST remain valid.
Malformed sources MUST remain visible without preventing neighboring sources from being checked.
The actual AI4X template MUST be a portable offline regression fixture.

**Implements**:
- `cpt-referenceaudit-flow-word-input-audit`
- `cpt-referenceaudit-algo-word-input-read`

**Touches**:
- API: `parse_docx`, `build_docx_parse_report`, `AuditPipeline.run_docx`, `run_docx_audit`
- Entities: `BibEntry`, `CitationContext`, `UnparsedEntry`, `AuditReport`

## 6. Acceptance Criteria

Evidence: `tests/test_docx.py` covers the following implemented behaviors; formal instruction-level
traceability remains outstanding.

- Word input works offline and routes full audits through the shared pipeline.
- Personal/corporate authors, source fields and identifiers survive mapping; duplicate Tags fail
  per record and other records continue.
- Split/grouped/simple fields, footnotes, deleted fields and bibliography suppression are covered.
- Bare citations remain cited with an explicit alignment issue rather than an empty LLM context.
- Missing structured sources and broken XML/fields fail explicitly.
- Scope warning appears on stderr and in report notes, including the free-form-only failure path.
- The actual AI4X fixture yields nine sources, ten citation fields, eleven per-source contexts,
  complete 34/68/14-author lists, and no missing/uncited entries in its offline CLI report.
