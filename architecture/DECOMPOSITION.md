# Decomposition: Reference Audit

<!-- toc -->

- [1. Overview](#1-overview)
- [2. Entries](#2-entries)
  - [2.1 Parsing - HIGH](#21-parsing---high)
  - [2.2 Identification & Verdict - HIGH](#22-identification--verdict---high)
- [3. Feature Dependencies](#3-feature-dependencies)

<!-- /toc -->

## 1. Overview

The Reference Audit DESIGN is decomposed by pipeline stage. The **Parsing** feature (offline
`.bib` + `.tex` parsing and identifier normalization) is the foundation every later stage builds
on. The downstream stages — identification against databases, the same-object matching rule and
3-way verdict, LLM adjudication, SQLite caching, web/book verification, and best-version/canonical
output — are now implemented in code. They are grouped here under a single **Identification &
Verdict** feature entry; that entry tracks code state but does not yet carry a dedicated governed
feature specification or instruction-level `@cpt` traceability (only the Parsing feature is traced
to code today).

**Decomposition Strategy**:
- Group by pipeline stage (parsing → sources/cache → matching/llm → report).
- Keep dependencies minimal and explicit: parsing is the foundation every later stage builds on.
- Assign every DESIGN component, principle, constraint, sequence, and data element to a feature entry
  so the design is fully covered.

## 2. Entries

**Overall implementation status:**

- [ ] `p1` - **ID**: `cpt-referenceaudit-status-overall`

### 2.1 [Parsing](features/parsing.md) - HIGH

- [x] `p1` - **ID**: `cpt-referenceaudit-feature-parsing`

- **Purpose**: Offline parse-only slice — parse `.bib` entries and `.tex` citations, normalize
  identifiers, and produce the `AuditReport` bookkeeping and deterministic issues that every later
  stage builds on.

- **Depends On**: None

- **Scope**:
  - Parse `.bib` into `BibEntry`s and detect commented preprint twins
  - Extract cited keys, resolve includes, report missing includes
  - Normalize DOI / ISBN / arXiv identifiers
  - Compute cited / uncited / cited-but-missing bookkeeping and per-entry issues

- **Out of scope**:
  - Any network or database access
  - Verdict computation and best-version selection

- **Requirements Covered**:

  - [x] `p1` - `cpt-referenceaudit-fr-parse-bib-tex`
  - [x] `p1` - `cpt-referenceaudit-nfr-offline-deterministic`

- **Design Principles Covered**:

  - [x] `p1` - `cpt-referenceaudit-principle-offline-first`

- **Design Constraints Covered**:

  - [x] `p1` - `cpt-referenceaudit-constraint-no-network-parse`

- **Domain Model Entities**:
  - BibEntry
  - Identifiers
  - EntryAudit
  - AuditReport

- **Design Components**:

  - [x] `p1` - `cpt-referenceaudit-component-parsing`
  - [x] `p1` - `cpt-referenceaudit-component-models`
  - [x] `p1` - `cpt-referenceaudit-component-report-cli`

- **API**:
  - `build_parse_report(tex_path, bib_path)`
  - `reference-audit audit`

- **Sequences**:

  - [x] `p1` - `cpt-referenceaudit-seq-build-parse-report`

- **Data**:
  - (none — parse slice is in-memory only)

### 2.2 [Identification & Verdict](DESIGN.md#32-component-model) - HIGH

- [ ] `p1` - **ID**: `cpt-referenceaudit-feature-identification`

  > **Status note:** the capability below is **implemented in code** (`sources/`, `matching/`,
  > `llm/`, `cache/`). This entry stays unchecked because it does not yet have a dedicated governed
  > FEATURE specification or instruction-level `@cpt` traceability — authoring `features/
  > identification.md` and tracing the matching code is the remaining governance work. Only the
  > Parsing feature is traced to code today.

- **Purpose**: Query bibliographic databases, score candidates with the SAME-OBJECT disambiguation
  rule, and return the 3-way verdict plus the canonical best version.

- **Depends On**: `cpt-referenceaudit-feature-parsing`

- **Scope**:
  - Modular source adapters (Crossref, OpenAlex, Semantic Scholar, arXiv, Open Library, publisher
    citation export, web page fetch) and SQLite caching
  - Feature scoring and the SAME-OBJECT clustering rule (formal + LLM tie-break)
  - 3-way verdict, hallucination screening, URL-only `@misc` web verification, Open Library book /
    edition resolution, DOI/ISBN backfill, best-version + canonical field output

- **Out of scope**:
  - Manuscript body rewriting

- **Requirements Covered** (all implemented in code; unchecked = pending `@cpt` tracing):

  - [ ] `p1` - `cpt-referenceaudit-fr-identify-artifact`
  - [ ] `p1` - `cpt-referenceaudit-fr-three-way-verdict`
  - [ ] `p1` - `cpt-referenceaudit-fr-hallucination-screen`
  - [ ] `p2` - `cpt-referenceaudit-fr-best-version-canonical`
  - [ ] `p2` - `cpt-referenceaudit-nfr-cached-calls`

- **Design Principles Covered**:

  - [ ] `p2` - `cpt-referenceaudit-principle-modular-sources`

- **Design Constraints Covered**:

  - [ ] `p1` - `cpt-referenceaudit-constraint-id-preference`

- **Domain Model Entities**:
  - SourceRecord
  - FeatureVector

- **Design Components**:

  - [ ] `p2` - `cpt-referenceaudit-component-sources`
  - [ ] `p1` - `cpt-referenceaudit-component-matching`
  - [ ] `p2` - `cpt-referenceaudit-component-llm`
  - [ ] `p2` - `cpt-referenceaudit-component-cache`

- **API**:
  - `reference_audit.pipeline.run_audit(...)` / `AuditPipeline.run(...)`

- **Sequences**:

  - [ ] `p2` - `cpt-referenceaudit-seq-identify-adjudicate`

- **Data**:

  - [ ] `p3` - `cpt-referenceaudit-db-cache`
  - `cpt-referenceaudit-dbtable-response-cache`

---

## 3. Feature Dependencies

```text
cpt-referenceaudit-feature-parsing
    ↓
    └─→ cpt-referenceaudit-feature-identification
```

**Dependency Rationale**:

- `cpt-referenceaudit-feature-identification` requires `cpt-referenceaudit-feature-parsing`:
  identification and verdicts operate on the normalized entries and identifiers the parse slice
  produces.
