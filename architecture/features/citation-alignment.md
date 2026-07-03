# Feature: Citation Alignment

<!-- toc -->

- [1. Feature Context](#1-feature-context)
  - [1.1 Overview](#11-overview)
  - [1.2 Purpose](#12-purpose)
  - [1.3 Actors](#13-actors)
  - [1.4 References](#14-references)
- [2. Actor Flows (CDSL)](#2-actor-flows-cdsl)
  - [Check one citation's alignment](#check-one-citations-alignment)
- [3. Processes / Business Logic (CDSL)](#3-processes--business-logic-cdsl)
  - [Classify one citing context against the abstract](#classify-one-citing-context-against-the-abstract)
- [4. States (CDSL)](#4-states-cdsl)
  - [Citation alignment status](#citation-alignment-status)
- [5. Definitions of Done](#5-definitions-of-done)
  - [Extract citing contexts from the manuscript](#extract-citing-contexts-from-the-manuscript)
  - [Source the cited work's abstract](#source-the-cited-works-abstract)
  - [Classify the citing context against the abstract](#classify-the-citing-context-against-the-abstract)
  - [Never report a false misalignment](#never-report-a-false-misalignment)
  - [Report citation alignment findings](#report-citation-alignment-findings)
- [6. Acceptance Criteria](#6-acceptance-criteria)

<!-- /toc -->

- [ ] `p2` - **ID**: `cpt-referenceaudit-featstatus-citation-alignment`

> **Status:** IMPLEMENTED, not yet `@cpt`-traced. The capability is built and covered by tests
> (`parsing/tex.py::parse_citation_contexts`, `alignmentcheck.py`, the `pipeline._check_alignment`
> wiring, and `report.py`). The checkboxes stay `[ ]` because instruction-level `@cpt` tracing to
> code is follow-on governance work — mirroring how the `sources`/`matching`/`llm`/`cache` components
> are implemented-but-untraced in [DESIGN](../DESIGN.md). Flip each box to `[x]` when its marker lands.

## 1. Feature Context

- [ ] `p2` - `cpt-referenceaudit-feature-citation-alignment`

### 1.1 Overview

An **advisory** check layered on top of [Identification & Verdict](identification.md): for each
in-text `\cite{X}` of a reference that resolved to `exactly_one`, compare the **citing context** —
the sentence(s) around the citation, i.e. the reason the author invokes X — against **X's abstract**,
and classify whether that usage is corroborated, contradicted, silent-in-abstract, or could-not-be
verified. Like `fieldcheck` and `bookcheck`, it only adds findings; it **never** changes the
identification verdict.

### 1.2 Purpose

This feature realizes citation-intent verification: catching references that are real (they resolve)
but are cited for a claim the cited work does not actually make. It is orchestrated by
`pipeline.AuditPipeline._check_alignment`, which runs after `_check_fields` on an `exactly_one`
match: it pairs each citing context (extracted from the `.tex` by `parsing/tex.py`) with the matched
artifact's abstract and classifies it (`alignmentcheck.py`, escalating to the LLM via `llm/`), caching
each decision. Each citation is judged in isolation; a failure leaves that citation `unverifiable`,
never a false `contradicted`, and never aborts the entry.

**Requirements**: `cpt-referenceaudit-fr-citation-alignment`

**Depends on requirements**: `cpt-referenceaudit-fr-identify-artifact`,
`cpt-referenceaudit-fr-parse-bib-tex`

**Principles**: `cpt-referenceaudit-principle-modular-sources`

### 1.3 Actors

| Actor | Role in Feature |
|-------|-----------------|
| `cpt-referenceaudit-actor-author` | Runs the audit to check their citations are used faithfully. |
| `cpt-referenceaudit-actor-reviewer` | Screens a submission for citations that misrepresent their sources. |
| `cpt-referenceaudit-actor-ai-agent` | Calls the check programmatically to self-verify a paper's citation usage. |
| `cpt-referenceaudit-actor-database` | The external source supplying the cited work's abstract. |

### 1.4 References

- **PRD**: [PRD.md](../PRD.md)
- **Design**: [DESIGN.md](../DESIGN.md)
- **Dependencies**: `cpt-referenceaudit-feature-identification`, `cpt-referenceaudit-feature-parsing`

## 2. Actor Flows (CDSL)

User-facing interaction: an actor requests the audit with alignment checking enabled and receives,
per citation of a resolved reference, an alignment finding with an evidence quote from the abstract.

**Use cases**: `cpt-referenceaudit-usecase-citation-alignment`

### Check one citation's alignment

- [ ] `p2` - **ID**: `cpt-referenceaudit-flow-alignment-check-citation`

**Actor**: `cpt-referenceaudit-actor-database`

**Success Scenarios**:
- A citation whose claim the abstract corroborates is reported `supported`.
- A citation whose claim the abstract contradicts is reported `contradicted` (the loud finding).

**Error Scenarios**:
- The cited work has no retrievable abstract, or did not resolve to `exactly_one` → the citation is
  `unverifiable` with the reason stated; never `contradicted`.
- An LLM/transport failure leaves the citation unresolved (reported, not cached, retried next run).

**Steps**:
1. [ ] - `p2` - **IF** alignment checking is disabled **OR** the verdict is not `exactly_one`, skip - `inst-guard`
2. [ ] - `p2` - Source the cited work's abstract from the matched artifact - `inst-abstract`
3. [ ] - `p2` - **FOR EACH** citing context of this entry, classify it against the abstract - `inst-classify`
4. [ ] - `p2` - Record the per-citation alignment findings on the entry - `inst-record`

## 3. Processes / Business Logic (CDSL)

### Classify one citing context against the abstract

- [ ] `p2` - **ID**: `cpt-referenceaudit-algo-alignment-classify`

Classify one citing context against the cited work's abstract into the alignment taxonomy. Called
per context by the flow's `inst-classify` step.

**Input**: One `CitationContext` and the cited work's abstract text.

**Output**: An `AlignmentFinding` (status + evidence quote + rationale + confidence).

**Steps**:
1. [ ] - `p2` - **IF** no abstract is available, **RETURN** `unverifiable` with the reason - `inst-no-abstract`
2. [ ] - `p2` - Ask the LLM (strict pydantic schema) whether the abstract supports the context - `inst-llm`
3. [ ] - `p2` - **RETURN** the mapped finding (supported / contradicted / not_in_abstract / unverifiable) - `inst-map`

## 4. States (CDSL)

### Citation alignment status

Each citing context settles to exactly one status. The crucial invariant is that a silent or absent
abstract yields `not_in_abstract` / `unverifiable`, **never** `contradicted` — the abstract is not
the full paper, so its silence is not evidence of misuse.

**States**: SUPPORTED, CONTRADICTED, NOT_IN_ABSTRACT, UNVERIFIABLE

**Initial State**: UNVERIFIABLE (until a classification is produced)

**Transition**: UNVERIFIABLE → {SUPPORTED, CONTRADICTED, NOT_IN_ABSTRACT} when the abstract is
available and the classifier decides; UNVERIFIABLE is retained (and retried next run) on any
transient LLM/transport failure or missing abstract.

## 5. Definitions of Done

### Extract citing contexts from the manuscript

- [ ] `p2` - **ID**: `cpt-referenceaudit-dod-alignment-extract-contexts`

The system **MUST** extract, per cited key, the citing context (the surrounding sentence(s)) for
each `\cite`-family occurrence in the `.tex` and its resolvable includes — offline and deterministic,
handling multi-key `\cite{a,b}` and repeated citation sites. This runs in the parse slice and adds no
network dependency.

**Implements**:
- `cpt-referenceaudit-flow-alignment-check-citation`

**Touches**:
- API: `reference_audit.parsing.tex.parse_citation_contexts(...)`
- Entities: `CitationContext`

### Source the cited work's abstract

- [ ] `p2` - **ID**: `cpt-referenceaudit-dod-alignment-fetch-abstract`

The system **MUST** obtain the cited work's abstract from the matched artifact's records (OpenAlex /
Semantic Scholar populate it), and when none is available **MUST** treat the citations as
`unverifiable` with that reason — never guess.

**Implements**:
- `cpt-referenceaudit-flow-alignment-check-citation`

**Touches**:
- Entities: `SourceRecord`, `MatchedArtifact`

### Classify the citing context against the abstract

- [ ] `p2` - **ID**: `cpt-referenceaudit-dod-alignment-classify`

The system **MUST** classify each citing context against the abstract into exactly one of
`supported`, `contradicted`, `not_in_abstract`, or `unverifiable`, using an LLM with a strict
pydantic-schema structured output, and **MUST** cache each decision by `(prompt, kind, model)`.

**Implements**:
- `cpt-referenceaudit-algo-alignment-classify`

**Touches**:
- API: `reference_audit.alignmentcheck.resolve_alignment_findings(...)`
- Entities: `AlignmentFinding`, `CitationAlignmentResult`

### Never report a false misalignment

- [ ] `p2` - **ID**: `cpt-referenceaudit-dod-alignment-never-false-misalign`

The system **MUST NOT** report `contradicted` when the abstract is merely silent on the claim, when no
abstract is available, when the reference did not resolve to `exactly_one`, or when the LLM/transport
failed. Every such case is `not_in_abstract` or `unverifiable`, with the reason stated; the failure is
not cached and each citation is judged in isolation.

**Implements**:
- `cpt-referenceaudit-algo-alignment-classify`

**Touches**:
- Entities: `AlignmentFinding`

### Report citation alignment findings

- [ ] `p2` - **ID**: `cpt-referenceaudit-dod-alignment-report`

The system **MUST** surface alignment findings in the text and JSON reports — `contradicted` loudly,
`not_in_abstract` / `unverifiable` as advisory notes — without altering the identification verdict.

**Implements**:
- `cpt-referenceaudit-flow-alignment-check-citation`

**Touches**:
- API: `reference_audit.report.render_text` / `render_json`
- Entities: `EntryAudit`, `AlignmentFinding`

## 6. Acceptance Criteria

- [ ] A citation whose claim the abstract corroborates is reported `supported`.
- [ ] A citation whose claim the abstract contradicts is reported `contradicted` with an evidence quote.
- [ ] A citation whose claim is absent from the abstract is `not_in_abstract`, never `contradicted`.
- [ ] A reference with no retrievable abstract (or not `exactly_one`) yields `unverifiable` with a stated reason.
- [ ] An LLM/transport failure leaves the citation unresolved and uncached, and never aborts the entry.
- [ ] Alignment checking never changes the identification verdict.
