# Decomposition: Reference Audit

<!-- toc -->

- [1. Overview](#1-overview)
- [2. Entries](#2-entries)
  - [2.1 Parsing - HIGH](#21-parsing---high)
  - [2.2 Identification & Verdict - HIGH](#22-identification--verdict---high)
- [3. Feature Dependencies](#3-feature-dependencies)

<!-- /toc -->

## 1. Overview

The Reference Audit DESIGN is decomposed by pipeline stage. The implemented foundation is the
**Parsing** feature (offline `.bib` + `.tex` parsing and identifier normalization). The remaining
stages — identification against databases, the same-object matching rule and 3-way verdict, LLM
adjudication, caching, and canonical output — are planned and tracked as separate, dependent
feature entries.

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

### 2.2 [Identification & Verdict](features/parsing.md) - HIGH

- [ ] `p1` - **ID**: `cpt-referenceaudit-feature-identification`

- **Purpose**: Query bibliographic databases, score candidates with the same-object disambiguation
  rule, and return the 3-way verdict plus the canonical best version. (Planned, M2–M5.)

- **Depends On**: `cpt-referenceaudit-feature-parsing`

- **Scope**:
  - Modular source adapters and SQLite caching
  - Feature scoring and same-object rule
  - 3-way verdict, hallucination screening, best-version + canonical output

- **Out of scope**:
  - Manuscript body rewriting

- **Requirements Covered**:

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
  - (planned)

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
