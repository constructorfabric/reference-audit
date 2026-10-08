# Decomposition: Reference Audit

<!-- toc -->

- [1. Overview](#1-overview)
- [2. Entries](#2-entries)
  - [2.1 Parsing - HIGH](#21-parsing---high)
  - [2.2 Identification & Verdict - HIGH](#22-identification--verdict---high)
  - [2.3 Citation Alignment - MEDIUM](#23-citation-alignment---medium)
  - [2.4 PDF Input - HIGH](#24-pdf-input---high)
- [3. Feature Dependencies](#3-feature-dependencies)

<!-- /toc -->

## 1. Overview

The Reference Audit DESIGN is decomposed by pipeline stage. The **Parsing** feature (offline
`.bib` + `.tex` parsing and identifier normalization) is the foundation every later stage builds
on. The downstream stages — identification against databases, the same-object matching rule and
3-way verdict, LLM adjudication, SQLite caching, web/book verification, and best-version/canonical
output — are implemented in code and governed by the dedicated **Identification & Verdict**
feature spec ([features/identification.md](features/identification.md)), which carries
instruction-level `@cpt` traceability into `pipeline.py`, `matching/`, and `cache/`.

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

### 2.2 [Identification & Verdict](features/identification.md) - HIGH

- [x] `p1` - **ID**: `cpt-referenceaudit-feature-identification`

- **Purpose**: Query bibliographic databases, score candidates with the SAME-OBJECT disambiguation
  rule, and return the 3-way verdict plus the canonical best version.

- **Depends On**: `cpt-referenceaudit-feature-parsing`

- **Scope**:
  - Modular source adapters (Crossref, OpenAlex, Semantic Scholar, arXiv, DBLP, Open Library, Google
    Books, publisher citation export, web page fetch) and SQLite caching
  - A local ClickHouse backend for Semantic Scholar, OpenAlex and DBLP, selected per run
    (`source_backend`), with backend-scoped caching
  - Feature scoring and the SAME-OBJECT clustering rule (formal + LLM tie-break)
  - 3-way verdict, hallucination screening, URL-only `@misc` web verification, Open Library book /
    edition resolution, DOI/ISBN backfill, best-version + canonical field output

- **Out of scope**:
  - Manuscript body rewriting

- **Requirements Covered**:

  - [x] `p1` - `cpt-referenceaudit-fr-identify-artifact`
  - [x] `p1` - `cpt-referenceaudit-fr-three-way-verdict`
  - [x] `p1` - `cpt-referenceaudit-fr-hallucination-screen`
  - [x] `p2` - `cpt-referenceaudit-fr-best-version-canonical`
  - [x] `p2` - `cpt-referenceaudit-nfr-cached-calls`

- **Design Principles Covered**:

  - [x] `p2` - `cpt-referenceaudit-principle-modular-sources`

- **Design Constraints Covered**:

  - [x] `p1` - `cpt-referenceaudit-constraint-id-preference`

- **Domain Model Entities**:
  - SourceRecord
  - FeatureVector

- **Design Components**:

  - [x] `p2` - `cpt-referenceaudit-component-sources`
  - [x] `p1` - `cpt-referenceaudit-component-matching`
  - [x] `p2` - `cpt-referenceaudit-component-llm`
  - [x] `p2` - `cpt-referenceaudit-component-cache`

- **API**:
  - `reference_audit.pipeline.run_audit(...)` / `AuditPipeline.run(...)`

- **Sequences**:

  - [x] `p2` - `cpt-referenceaudit-seq-identify-adjudicate`

- **Data**:

  - [x] `p3` - `cpt-referenceaudit-db-cache`
  - `cpt-referenceaudit-dbtable-response-cache`

### 2.3 [Citation Alignment](features/citation-alignment.md) - MEDIUM

- [ ] `p2` - **ID**: `cpt-referenceaudit-feature-citation-alignment`

- **Purpose**: For each in-text citation of an `exactly_one`-resolved reference, compare the citing
  context against the cited work's abstract and classify the usage — an advisory check that never
  changes the identification verdict. **IMPLEMENTED (not yet @cpt-traced).**

- **Depends On**: `cpt-referenceaudit-feature-identification`, `cpt-referenceaudit-feature-parsing`

- **Scope**:
  - Extract per-key citing contexts from the `.tex` (offline, in the parse slice)
  - Source the cited work's abstract from the matched artifact
  - Classify each context (supported / contradicted / not_in_abstract / unverifiable) via the LLM
  - Report alignment findings without altering the verdict

- **Out of scope**:
  - Full-text claim verification (abstract-only in v1)

- **Requirements Covered**:

  - [ ] `p2` - `cpt-referenceaudit-fr-citation-alignment`

- **Domain Model Entities**:
  - CitationContext
  - AlignmentFinding

- **Design Components**:

  - [ ] `p2` - `cpt-referenceaudit-component-alignment`

- **API**:
  - `reference_audit.alignmentcheck.resolve_alignment_findings(...)`
  - `reference_audit.parsing.tex.parse_citation_contexts(...)`

- **Sequences**:

  - [ ] `p2` - `cpt-referenceaudit-seq-citation-alignment`

---

### 2.4 [PDF Input](features/pdf-input.md) - HIGH

- [ ] `p1` - **ID**: `cpt-referenceaudit-feature-pdf-input`

- **Purpose**: Accept a PDF as the whole audited input — a locally-run GROBID converts it to TEI, and a
  pure mapper turns that TEI into the `BibEntry`s and `CitationContext`s the rest of the pipeline
  already consumes, so nothing downstream changes. **IMPLEMENTED (not yet @cpt-traced).**

- **Depends On**: `cpt-referenceaudit-feature-parsing`

- **Scope**:
  - Send a PDF to an operator-supplied GROBID instance and return its TEI (one request per run)
  - Map TEI `<biblStruct>` elements to `BibEntry` via the shared `entry_from_fields` seam
  - Map in-text `<ref type="bibr">` markers to `CitationContext` via the shared sentence definition
  - Report per-reference extraction gaps as entry issues, and input-level caveats as report notes
  - Name every failure mode distinctly instead of degrading to an empty reference list
  - Measure extraction fidelity against documents compiled from a known `.bib`

- **Out of scope**:
  - Managing the GROBID container (start/stop/health administration)
  - Scanned or image-only PDFs (no OCR)
  - Extractors other than GROBID, and PDF *output*

- **Requirements Covered**:

  - [ ] `p1` - `cpt-referenceaudit-fr-audit-pdf`
  - [ ] `p1` - `cpt-referenceaudit-nfr-extraction-fidelity`

- **Domain Model Entities**:
  - BibEntry
  - CitationContext
  - AuditReport

- **Design Principles Covered**:

  - [ ] `p1` - `cpt-referenceaudit-principle-pure-tei-mapping`

- **Design Constraints Covered**:

  - [ ] `p1` - `cpt-referenceaudit-constraint-grobid-local-only`

- **Design Components**:

  - [ ] `p1` - `cpt-referenceaudit-component-pdf`

- **API**:
  - `reference_audit.pipeline.run_pdf_audit(...)`
  - `reference_audit.pipeline.build_pdf_parse_report(...)`
  - `reference_audit.pdf.tei.parse_tei(...)`
  - `reference_audit.pdf.grobid.GrobidClient`

- **Sequences**:

  - [ ] `p1` - `cpt-referenceaudit-seq-audit-pdf`

---

## 3. Feature Dependencies

```text
cpt-referenceaudit-feature-parsing
    ↓
    ├─→ cpt-referenceaudit-feature-pdf-input
    ├─→ cpt-referenceaudit-feature-identification
    │       ↓
    └─────→ cpt-referenceaudit-feature-citation-alignment
```

**Dependency Rationale**:

- `cpt-referenceaudit-feature-identification` requires `cpt-referenceaudit-feature-parsing`:
  identification and verdicts operate on the normalized entries and identifiers the parse slice
  produces.
- `cpt-referenceaudit-feature-citation-alignment` requires both: it needs the citing contexts from
  the parse slice and the resolved artifact (plus its abstract) from identification.
- `cpt-referenceaudit-feature-pdf-input` requires `cpt-referenceaudit-feature-parsing`: it is an
  alternative *front end* that emits the parse slice's own models, reusing its entry-construction seam
  and its sentence definition. It is a sibling of identification rather than a dependency of it —
  identification, citation alignment and reporting consume its output unchanged, which is the whole
  point of mapping into the existing models.
