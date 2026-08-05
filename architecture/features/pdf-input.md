# Feature: PDF Input


<!-- toc -->

- [1. Feature Context](#1-feature-context)
  - [1.1 Overview](#11-overview)
  - [1.2 Purpose](#12-purpose)
  - [1.3 Actors](#13-actors)
  - [1.4 References](#14-references)
- [2. Actor Flows (CDSL)](#2-actor-flows-cdsl)
  - [Audit a PDF](#audit-a-pdf)
- [3. Processes / Business Logic (CDSL)](#3-processes--business-logic-cdsl)
  - [Map TEI references to entries](#map-tei-references-to-entries)
  - [Map in-text markers to citing contexts](#map-in-text-markers-to-citing-contexts)
- [4. States (CDSL)](#4-states-cdsl)
  - [Citation linking availability](#citation-linking-availability)
- [5. Definitions of Done](#5-definitions-of-done)
  - [GROBID failures are named, never degraded](#grobid-failures-are-named-never-degraded)
  - [TEI maps to the same models as a .bib](#tei-maps-to-the-same-models-as-a-bib)
  - [Citing contexts mean the same thing as .tex ones](#citing-contexts-mean-the-same-thing-as-tex-ones)
  - [A PDF is accepted as the whole input](#a-pdf-is-accepted-as-the-whole-input)
  - [Inapplicable bookkeeping is declared, not zeroed](#inapplicable-bookkeeping-is-declared-not-zeroed)
  - [Extraction fidelity is measured against a compiled oracle](#extraction-fidelity-is-measured-against-a-compiled-oracle)
- [6. Acceptance Criteria](#6-acceptance-criteria)

<!-- /toc -->

- [ ] `p1` - **ID**: `cpt-referenceaudit-featstatus-pdf-input`

> **Status:** IMPLEMENTED, not yet `@cpt`-traced. The capability is built and covered by tests
> (`pdf/grobid.py`, `pdf/tei.py`, the `inputs.py`/`cli.py`/`pipeline.py` wiring, the offline mapper
> tests in `tests/test_tei.py` and `tests/test_grobid_client.py`, the stubbed end-to-end run in
> `tests/test_pdf_pipeline.py`, and the live compiled-PDF oracle in `tests/test_pdf_extraction.py`).
> The checkboxes stay `[ ]` because instruction-level `@cpt` tracing to code is follow-on governance
> work — mirroring [Citation Alignment](citation-alignment.md) and the untraced
> `sources`/`matching`/`llm`/`cache` components in [DESIGN](../DESIGN.md). Flip each box to `[x]` when
> its marker lands.

## 1. Feature Context

- [ ] `p1` - `cpt-referenceaudit-feature-pdf-input`

### 1.1 Overview

A PDF as the whole audited input. A locally-run GROBID converts the PDF to TEI XML; a pure mapper turns
that TEI into the same `BibEntry` and `CitationContext` records the `.bib`/`.tex` front end produces.
Identification, matching, verdicts, citation alignment and reporting are untouched — the PDF path
replaces only where the records come from.

### 1.2 Purpose

For mass automated processing the artifact actually available is a PDF, with no authored LaTeX sources
anywhere. This feature removes that precondition. It is orchestrated by
`pipeline.build_pdf_parse_report`, which calls `pdf.grobid.GrobidClient.fulltext_tei` and
`pdf.tei.parse_tei`, then reuses `pipeline._parse_issues` and the ordinary per-entry audit.

Two properties are load-bearing rather than incidental. First, the extraction is honest about itself:
records GROBID could not read are reported as uncheckable instead of being searched on or quietly
dropped, and bookkeeping that a PDF cannot support (commented twins, unresolved includes, and citedness
when no marker links) is declared inapplicable or unknown rather than reported as zero. Second, the
network call and the mapping are separate modules, so extraction correctness is verifiable offline from
recorded TEI.

**Requirements**: `cpt-referenceaudit-fr-audit-pdf`, `cpt-referenceaudit-nfr-extraction-fidelity`

**Principles**: `cpt-referenceaudit-principle-pure-tei-mapping`

### 1.3 Actors

| Actor | Role in Feature |
|-------|-----------------|
| `cpt-referenceaudit-actor-author` | Audits a paper they have only as a PDF. |
| `cpt-referenceaudit-actor-ai-agent` | Screens PDFs in bulk with no authored sources available. |
| `cpt-referenceaudit-actor-grobid` | Converts the PDF to TEI; supplied by the operator, not managed here. |

### 1.4 References

- **PRD**: [PRD.md](../PRD.md)
- **Design**: [DESIGN.md](../DESIGN.md)
- **Dependencies**: [Parsing](parsing.md) — the entry-construction seam and the sentence definition

## 2. Actor Flows (CDSL)

User-facing interaction: an author audits a PDF and receives the same `AuditReport` they would get
from a `.bib` + `.tex` pair, plus explicit notes on what the extraction could not recover.

**Use cases**: `cpt-referenceaudit-usecase-audit-pdf`

### Audit a PDF

- [ ] `p1` - **ID**: `cpt-referenceaudit-flow-pdf-input-audit-pdf`

**Actor**: `cpt-referenceaudit-actor-author`

**Success Scenarios**:
- A born-digital PDF with a bibliography yields an `AuditReport` whose references are identified and
  screened exactly as `.bib`-derived ones are.
- References GROBID read incompletely are still reported, with the reason and the raw reference text.

**Error Scenarios**:
- GROBID unreachable, persistently failing, or unable to parse this PDF: the run stops with that
  specific reason and the command to start the service — never a partial bibliography.
- TEI containing no reference list: the run stops with "nothing to audit", not a clean bill of health.
- No in-text marker linkable to a bibliography entry: citedness is reported unknown, and no reference
  is listed as uncited.

**Steps**:
1. [ ] - `p1` - Classify the CLI arguments as a PDF input, rejecting a `.pdf` combined with a `.bib` and `--no-network` with a `.pdf` (`inputs.resolve_input`) - `inst-resolve-input`
2. [ ] - `p1` - Health-check GROBID, then upload the PDF once and return its TEI (`GrobidClient.fulltext_tei`) - `inst-fetch-tei`
3. [ ] - `p1` - Map TEI `<biblStruct>` elements to `BibEntry` records (`pdf.tei.parse_tei`) - `inst-map-references`
4. [ ] - `p1` - Map in-text `<ref type="bibr">` markers to per-reference `CitationContext`s - `inst-map-contexts`
5. [ ] - `p1` - **FOR EACH** reference collect deterministic issues plus its extraction failures - `inst-collect-issues`
6. [ ] - `p1` - **RETURN** the assembled `AuditReport` with `input_kind`, `citation_linking` and input notes - `inst-build-report`

## 3. Processes / Business Logic (CDSL)

### Map TEI references to entries

- [ ] `p1` - **ID**: `cpt-referenceaudit-algo-pdf-input-tei-references`

Each `<biblStruct>` becomes a flat BibTeX-shaped field dict handed to `parsing.bib.entry_from_fields`,
so identifier normalization, venue selection and cleaning are defined exactly once across both front
ends. Three decisions carry the weight:

- **Titles depend on structure.** A `monogr` title at `level="j"` is always a venue. At `level="m"` it
  is a venue only when an `analytic` title also exists (the reference is a contribution *inside* that
  monograph); with no analytic title the reference *is* the monograph, so its title is the work's own.
  Treating a cited book's title as a venue leaves the entry title-less and unmatchable.
- **Entry type is inferred from structure**, because TEI carries none and `entry_type` decides source
  routing. Where structure is ambiguous the type is left UNKNOWN rather than guessed.
- **A DOI is extended, never invented.** A DOI broken across a line arrives truncated, and a truncated
  DOI is a *different* identifier that may resolve to another document. Repair therefore only ever
  extends what GROBID read, only from that reference's own text, and only within a bounded length — an
  unbounded extension runs into the next reference when GROBID has merged two.

**Input**: One GROBID TEI document.

**Output**: `BibEntry` records with synthetic keys (the TEI `xml:id`, which the in-text markers point
at), plus per-record reasons any of them came out incomplete.

### Map in-text markers to citing contexts

- [ ] `p1` - **ID**: `cpt-referenceaudit-algo-pdf-input-tei-contexts`

Body paragraphs are flattened to plain text with each citation marker's own surface text removed — the
marker is not part of the citing claim, exactly as the LaTeX path substitutes the `\cite` macro away —
while its offset is kept so the surrounding sentence can be taken with the shared
`parsing.context.sentence_span`. Both front ends therefore mean the same thing by "the citing
sentence", which is what makes an alignment finding comparable across input formats.

Markers GROBID left unlinked are counted, never attached to a guessed reference. A marker pointing at
an id no bibliography entry declares is reported as inconsistent TEI rather than as the author's
dangling citation.

**Input**: The same TEI document.

**Output**: `CitationContext`s per reference key, with 0-based ordinals in document order; plus counts
of unlinked and dangling markers.

## 4. States (CDSL)

### Citation linking availability

- [ ] `p1` - **ID**: `cpt-referenceaudit-state-pdf-input-extraction`

Whether cited/uncited bookkeeping can be reported at all is a property of the extraction, not of the
document, so it is carried explicitly on the report.

**States**: AVAILABLE, UNAVAILABLE

**Initial State**: UNAVAILABLE

**Transition**: UNAVAILABLE → AVAILABLE when at least one in-text marker resolves to a bibliography
entry. While UNAVAILABLE the uncited list is suppressed and the report says citedness is unknown —
reporting every reference as uncited would assert something the extraction does not know.

## 5. Definitions of Done

### GROBID failures are named, never degraded

- [ ] `p1` - **ID**: `cpt-referenceaudit-dod-pdf-input-fail-loudly`

The system **MUST** distinguish an unreachable service, a persistently failing one, an unparseable PDF,
malformed TEI, and a TEI with no reference list — each with its own error and message — and **MUST NOT**
turn any of them into an empty or partial reference list. An unreachable service **MUST** name the
command that starts one.

**Implements**:
- `cpt-referenceaudit-flow-pdf-input-audit-pdf`

**Constraints**: `cpt-referenceaudit-constraint-grobid-local-only`

**Touches**:
- API: `GrobidClient.check_alive`, `GrobidClient.fulltext_tei`, `build_pdf_parse_report`
- Entities: `GrobidError`, `EmptyBibliographyError`

### TEI maps to the same models as a .bib

- [ ] `p1` - **ID**: `cpt-referenceaudit-dod-pdf-input-map-references`

The system **MUST** produce `BibEntry` records through the shared `entry_from_fields` seam, infer an
entry type from TEI structure, and never emit an identifier it did not read from the reference's own
text.

**Implements**:
- `cpt-referenceaudit-algo-pdf-input-tei-references`

**Touches**:
- API: `pdf.tei.parse_tei`, `parsing.bib.entry_from_fields`
- Entities: `BibEntry`, `Identifiers`

### Citing contexts mean the same thing as .tex ones

- [ ] `p1` - **ID**: `cpt-referenceaudit-dod-pdf-input-map-contexts`

The system **MUST** extract citing contexts using the same sentence definition as the LaTeX path, with
0-based per-reference ordinals in document order, and **MUST NOT** emit an empty context or attach one
to a reference that has no marker.

**Implements**:
- `cpt-referenceaudit-algo-pdf-input-tei-contexts`

**Touches**:
- API: `pdf.tei.parse_tei`, `parsing.context.sentence_span`
- Entities: `CitationContext`

### A PDF is accepted as the whole input

- [ ] `p1` - **ID**: `cpt-referenceaudit-dod-pdf-input-cli-argument`

The system **MUST** accept `audit <paper.pdf>` with no second argument, and **MUST** reject a `.pdf`
combined with a `.bib`, and `--no-network` combined with a `.pdf`, with a message naming the invocation
that works.

**Implements**:
- `cpt-referenceaudit-flow-pdf-input-audit-pdf`

**Touches**:
- API: `inputs.resolve_input`, `cli.audit`
- Entities: `PdfInput`, `TexBibInput`

### Inapplicable bookkeeping is declared, not zeroed

- [ ] `p1` - **ID**: `cpt-referenceaudit-dod-pdf-input-honest-bookkeeping`

The system **MUST** mark a PDF-derived report as such, **MUST** suppress the uncited list when no
citation marker could be linked, and **MUST NOT** report commented twins or unresolved includes as
counts for an input that cannot have them.

**Implements**:
- `cpt-referenceaudit-flow-pdf-input-audit-pdf`
- `cpt-referenceaudit-state-pdf-input-extraction`

**Touches**:
- API: `build_pdf_parse_report`, `report.render_text`
- Entities: `AuditReport.input_kind`, `AuditReport.citation_linking`, `AuditReport.notes`

### Extraction fidelity is measured against a compiled oracle

- [ ] `p1` - **ID**: `cpt-referenceaudit-dod-pdf-input-compiled-oracle`

Extraction **MUST** be measured against test documents compiled from their own `.bib`, so that `.bib`
is ground truth, with pinned floors that fail on regression. Missing inputs for a compile **MUST** be
reported all at once and never stubbed; the one permitted substitution is placeholder figures, which
**MUST** be disclosed in the build report and in the recorded fixture's provenance.

**Implements**:
- `cpt-referenceaudit-algo-pdf-input-tei-references`
- `cpt-referenceaudit-algo-pdf-input-tei-contexts`

**Touches**:
- API: `tests/pdf_fixtures.py` (preflight, compile, TEI capture), `tests/pdf_oracle.py` (matching)
- Entities: `MissingDocumentInputsError`, `LatexCompileError`, `Comparison`

## 6. Acceptance Criteria

- [x] `reference-audit audit paper.pdf` audits a PDF with no `.bib` and no `.tex`.
- [x] An unreachable GROBID exits non-zero quoting the exact `podman run` command; a TEI with no
      reference list exits non-zero saying so.
- [x] A reference with neither a title nor an identifier is reported as uncheckable, and no source is
      queried for it.
- [x] When no in-text marker links, no reference is reported as uncited and the report says citedness
      is unknown.
- [x] A DOI cut short by a line break is extended from the reference's own text or left as read —
      never replaced by a guess.
- [x] The single-column pilot extracts all 28 printed references in printed order, with no wrong or
      missing years and every reference's citations linked.
- [x] Citing contexts recovered from a PDF match the LaTeX-derived ones at median similarity 1.00.
- [x] Compiling a document with a missing input reports every missing file in one pass and generates
      no stubs.
