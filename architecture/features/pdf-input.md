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
  - [Read the BibTeX records a PDF embeds](#read-the-bibtex-records-a-pdf-embeds)
  - [Read the reference list with a language model](#read-the-reference-list-with-a-language-model)
  - [Re-read with the escalation model](#re-read-with-the-escalation-model)
- [4. States (CDSL)](#4-states-cdsl)
  - [Citation linking availability](#citation-linking-availability)
- [5. Definitions of Done](#5-definitions-of-done)
  - [GROBID failures are named, never degraded](#grobid-failures-are-named-never-degraded)
  - [TEI maps to the same models as a .bib](#tei-maps-to-the-same-models-as-a-bib)
  - [Citing contexts mean the same thing as .tex ones](#citing-contexts-mean-the-same-thing-as-tex-ones)
  - [A PDF is accepted as the whole input](#a-pdf-is-accepted-as-the-whole-input)
  - [Inapplicable bookkeeping is declared, not zeroed](#inapplicable-bookkeeping-is-declared-not-zeroed)
  - [Extraction fidelity is measured against a compiled oracle](#extraction-fidelity-is-measured-against-a-compiled-oracle)
  - [Embedded records are preferred, and checked against the printed list](#embedded-records-are-preferred-and-checked-against-the-printed-list)
  - [A language-model reading is used only when complete](#a-language-model-reading-is-used-only-when-complete)
  - [Entries a misreading can explain are read again](#entries-a-misreading-can-explain-are-read-again)
- [6. Acceptance Criteria](#6-acceptance-criteria)

<!-- /toc -->

- [ ] `p1` - **ID**: `cpt-referenceaudit-featstatus-pdf-input`

> **Status:** IMPLEMENTED, mostly not yet `@cpt`-traced. The capability is built and covered by tests
> (`pdf/grobid.py`, `pdf/tei.py`, the `inputs.py`/`cli.py`/`pipeline.py` wiring, the offline mapper
> tests in `tests/test_tei.py` and `tests/test_grobid_client.py`, the stubbed end-to-end run in
> `tests/test_pdf_pipeline.py`, the live compiled-PDF oracle in `tests/test_pdf_extraction.py`, the
> embedded-records path in `pdf/embedded.py` with `tests/test_embedded.py`, and the language-model
> reader in `pdf/llm_refs.py`, `LLMClient.read_pdf` and `AuditPipeline.run_pdf` with
> `tests/test_llm_refs.py` and `tests/test_llm_pdf_pipeline.py`). The language-model reader is traced: its two flow
> instructions carry `@cpt` block markers and its two algorithms scope markers, and those four are
> checked (its definitions of done are not, pending DoD evidence markers). The other checkboxes stay `[ ]` because instruction-level `@cpt` tracing to code is follow-on governance
> work — mirroring [Citation Alignment](citation-alignment.md) and the untraced
> `sources`/`matching`/`llm`/`cache` components in [DESIGN](../DESIGN.md). Flip each box to `[x]` when
> its marker lands.

## 1. Feature Context

- [ ] `p1` - `cpt-referenceaudit-feature-pdf-input`

### 1.1 Overview

A PDF as the whole audited input. By default a language model reads the PDF and transcribes its printed
reference list into a constrained schema; pure checks decide whether the reading can be used, and a
stronger model re-reads what they, or the audit, flag. Alternatively (`--pdf-extractor grobid`) a
locally-run GROBID converts the PDF to TEI XML, and a pure mapper turns that TEI into the same
`BibEntry` and `CitationContext` records the `.bib`/`.tex` front end produces. Identification,
matching, verdicts, citation alignment and reporting are untouched — the PDF path replaces only where
the records come from.

A PDF can also carry the authored records itself: one made with the AI4X conference template embeds
the BibTeX records of its cited works as the attachment `cited-references.bib.hex`. Such a PDF is read
from those records, parsed exactly as a `.bib`, and GROBID is not involved.

### 1.2 Purpose

For mass automated processing the artifact actually available is a PDF, with no authored LaTeX sources
anywhere. This feature removes that precondition. It is orchestrated by `AuditPipeline.run_pdf`: with
the default extractor through `LLMClient.read_pdf` and `pdf.llm_refs`, with GROBID through
`pipeline.build_pdf_parse_report`, which calls `pdf.grobid.GrobidClient.fulltext_tei` and
`pdf.tei.parse_tei`. Both then reuse `pipeline._parse_issues` and the ordinary per-entry audit. The
language-model reader was chosen over GROBID after measurement (`docs/archive/llm-pdf-references.md`):
on 38 hand-transcribed conference abstracts it recovered every printed reference where GROBID
recovered 93%, and GROBID mis-segmented the author names of one reference in five.

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
| `cpt-referenceaudit-actor-llm-reader` | Reads the printed reference list from the PDF (default extractor). |
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
- References read incompletely are still reported, with the reason and the raw reference text.
- An entry whose audit a misreading can explain is read again by the escalation model, and audited
  again if read differently; the report says which model's reading each entry is.

**Error Scenarios**:
- Neither model produced a usable reading (cut short, inconsistent, refused, failed): the run stops
  with each model's reason — never a partial bibliography.
- Both models found no reference list: the run stops with "nothing to audit".
- GROBID unreachable, persistently failing, or unable to parse this PDF: the run stops with that
  specific reason and the command to start the service — never a partial bibliography.
- TEI containing no reference list: the run stops with "nothing to audit", not a clean bill of health.
- No in-text marker linkable to a bibliography entry: citedness is reported unknown, and no reference
  is listed as uncited.

**Steps**:
1. [ ] - `p1` - Classify the CLI arguments as a PDF input, rejecting a `.pdf` combined with a `.bib`, and `--no-network` with a `.pdf` that embeds no BibTeX records (`inputs.resolve_input`, `cli.audit`) - `inst-resolve-input`
2. [ ] - `p1` - **IF** the PDF embeds the BibTeX records of its cited works and `--ignore-embedded` is not given, parse them as a `.bib`, look each title up in the printed reference list, and **RETURN** that `AuditReport` (`pipeline.build_embedded_parse_report`); a malformed attachment continues below with its reason as an input note - `inst-read-embedded`
3. [x] - `p1` - **IF** the extractor is the language model, read the reference list with the extraction model; **IF** that reading fails `list_problems`, gives no answer, or finds no list, read it with the escalation model, and fail with both reasons when that reading is unusable too (`AuditPipeline._read_pdf_with_llm`) - `inst-llm-read`
4. [x] - `p1` - Map the reading to `BibEntry` records, apply the DOI checks, re-read the flagged entries with the escalation model, and **RETURN** the `AuditReport` with citedness unavailable (`pdf.llm_refs.to_entries`, `AuditPipeline._check_dois`, `build_llm_parse_report`); after the audit, re-read the entries a misreading can explain (`AuditPipeline._reread_flagged`) - `inst-llm-doi-checks`
5. [ ] - `p1` - Health-check GROBID, then upload the PDF once and return its TEI (`GrobidClient.fulltext_tei`) - `inst-fetch-tei`
6. [ ] - `p1` - Map TEI `<biblStruct>` elements to `BibEntry` records (`pdf.tei.parse_tei`) - `inst-map-references`
7. [ ] - `p1` - Map in-text `<ref type="bibr">` markers to per-reference `CitationContext`s - `inst-map-contexts`
8. [ ] - `p1` - **FOR EACH** reference collect deterministic issues plus its extraction failures - `inst-collect-issues`
9. [ ] - `p1` - **RETURN** the assembled `AuditReport` with `input_kind`, `citation_linking` and input notes - `inst-build-report`

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

### Read the BibTeX records a PDF embeds

- [ ] `p1` - **ID**: `cpt-referenceaudit-algo-pdf-input-embedded-records`

The AI4X template's bibliography style (`ai4x.bst`, `unsrturl.bst` with one addition) writes each cited
entry's BibTeX record into the `.bbl`, and its class embeds the records as the PDF attachment
`cited-references.bib.hex`. They are the cited entries only, in printed order, with only the printed
fields plus identifiers, and with `@string` macros and `crossref` resolved by BibTeX. They are
hex-encoded because they pass through TeX, where a raw `%`, `#` or `\` would break them; whitespace in
the payload is insignificant, since BibTeX and TeX wrap its lines.

The decoded records go through `parsing.bib.parse_bib_text`, so an entry is built exactly as from the
`.bib` it came from and shares its content hash and verdict cache. Every entry is cited.

A record is what the author compiled, which a hand-edited `.bbl` can make differ from what is printed.
Each record's title is therefore looked up in the PDF's text after its last References heading: the
title is split at math and LaTeX commands, whose typeset form differs from the source, and every
segment of four or more letters must appear in the printed text, compared as lower-case ASCII letters
only so that hyphenation, line breaks, ligatures and accents fall away. A title with no such segment
is not judged.

**Input**: A PDF.

**Output**: The decoded `.bib` text, or nothing; a reason when an attachment exists but cannot be used
(the PDF is unreadable, the attachment is not hex, is truncated, or holds no entry). Then `BibEntry`
records, each with an issue if its title was not found in the printed reference list.

### Read the reference list with a language model

- [x] `p1` - **ID**: `cpt-referenceaudit-algo-pdf-input-llm-references`

The model gets the PDF (text layer and page images) and answers in `PrintedReferenceList`, a strict
schema enforced while decoding. Four decisions carry the weight:

- **Transcribe, never complete.** The instructions forbid looking up, correcting or completing a
  reference, and each entry's `raw_text` comes first, so the fields are read off the transcription.
- **Constraints with an escape hatch.** DOI, arXiv id, ISBN and URL carry regex patterns and the year a
  range, and every one of them is nullable: when what is printed does not fit, the field is null and
  the entry's `doubts` say why. A constraint without that hatch makes the model write a well-formed
  wrong value instead.
- **Completeness is checked, not assumed.** The model names the start of the last printed entry
  before transcribing and reports its coverage after. `list_problems` rejects a reading that reports
  stopping early, whose last entry is not the one it named, or that contradicts itself about having a
  list — a model can stop early inside a completed, schema-valid response.
- **A DOI must be on the page.** `doi_problems` flags a DOI that does not occur in the PDF's text layer
  (ignoring whitespace and hyphens, which text layers lose) and a DOI carried by two references with
  different titles (one entry's tail spliced onto another).

Each reference becomes a `BibEntry` through `parsing.bib.entry_from_fields`, keyed `r1`, `r2`, … in
printed order, with the printed position, the transcribed text and the reading model in `raw_fields`
(none of which enters the content hash) and a printed "et al." as the `others` marker. Usable readings
are cached by (PDF hash, model, schema version).

**Input**: A PDF and the extraction model.

**Output**: `BibEntry` records and per-entry issues (doubts, DOI problems), or the reasons the reading
cannot be used.

### Re-read with the escalation model

- [x] `p1` - **ID**: `cpt-referenceaudit-algo-pdf-input-llm-escalation`

The escalation model reads the whole PDF at most once per document, on first need, and that reading
serves every escalation of the document:

- **An unusable list** is replaced by the escalation model's reading when that one is usable; a 'no
  list' answer is confirmed by it.
- **A DOI flagged before the audit** is replaced by the escalation model's reading of the same printed
  reference when that reading has no DOI problem; otherwise the entry stays as read, with the problem as
  an issue.
- **An entry whose audit a misreading can explain** — 'no match', a contradiction, or an error finding
  on its title, authors, year or DOI — is compared with the escalation model's reading of the same
  printed reference (`counterpart`, by transcribed text, printed position breaking ties). If the two
  differ (`reading_differences`, ignoring name order and spacing), the escalation model's reading
  replaces the entry under the same key and position and is audited again; if not, the finding stands
  and concerns the reference as printed. An entry left unresolved because a source failed is not
  re-read.

**Input**: The audited report and the document's readings.

**Output**: The report with re-read entries replaced and audited, each re-read stated as an issue, and a
`pdf_reading` summary (who read the list, how many entries were re-read or replaced, token usage).

## 4. States (CDSL)

### Citation linking availability

- [ ] `p1` - **ID**: `cpt-referenceaudit-state-pdf-input-extraction`

Whether cited/uncited bookkeeping can be reported at all is a property of the extraction, not of the
document, so it is carried explicitly on the report.

**States**: AVAILABLE, UNAVAILABLE

**Initial State**: UNAVAILABLE

**Transition**: UNAVAILABLE → AVAILABLE when at least one in-text marker resolves to a bibliography
entry. A language-model reading reads no markers, so it stays UNAVAILABLE. While UNAVAILABLE the uncited list is suppressed and the report says citedness is unknown —
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
combined with a `.bib`, and `--no-network` combined with a `.pdf` that embeds no BibTeX records, with a
message naming the invocation that works.

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

### Embedded records are preferred, and checked against the printed list

- [ ] `p1` - **ID**: `cpt-referenceaudit-dod-pdf-input-embedded-records`

The system **MUST** audit a PDF from the BibTeX records it embeds, without contacting GROBID, unless
`--ignore-embedded` is given; **MUST** fall back to GROBID, with the reason as an input note, when the
attachment exists but cannot be used; **MUST** report as an issue each record whose title is not found
in the printed reference list; and **MUST** declare citation alignment unchecked once, for the
document, since the records carry no citing sentences.

**Implements**:
- `cpt-referenceaudit-flow-pdf-input-audit-pdf`
- `cpt-referenceaudit-algo-pdf-input-embedded-records`

**Touches**:
- API: `pdf.embedded.read_embedded_references`, `pdf.embedded.title_printed`,
  `pipeline.build_embedded_parse_report`, `AuditPipeline.run_pdf`, `parsing.bib.parse_bib_text`
- Entities: `EmbeddedReferences`, `AuditReport.input_kind`, `AuditConfig.use_embedded_references`

### A language-model reading is used only when complete

- [ ] `p1` - **ID**: `cpt-referenceaudit-dod-pdf-input-llm-reading`

The system **MUST** read a PDF that embeds no BibTeX records with the extraction model unless
`--pdf-extractor grobid` is given; **MUST NOT** audit a reading that `list_problems` rejects; **MUST**
then read with the escalation model and **MUST** fail with each model's reason when neither reading is
usable; **MUST** confirm a 'no list' answer with the escalation model before reporting that there is
nothing to audit; **MUST** re-read, before any audit, an entry whose DOI is absent from the PDF's text
or shared with a differently-titled entry; and **MUST** declare citedness unknown and citation
alignment unchecked for the document.

**Implements**:
- `cpt-referenceaudit-flow-pdf-input-audit-pdf`
- `cpt-referenceaudit-algo-pdf-input-llm-references`

**Constraints**: `cpt-referenceaudit-constraint-llm-reading-complete`

**Touches**:
- API: `LLMClient.read_pdf`, `pdf.llm_refs.list_problems`, `pdf.llm_refs.doi_problems`,
  `pdf.llm_refs.to_entries`, `AuditPipeline.run_pdf`, `build_llm_parse_report`, `cli.audit`
  (`--pdf-extractor`)
- Entities: `PrintedReferenceList`, `PdfReadError`, `AuditConfig.pdf_extractor`,
  `AuditConfig.pdf_extraction_model`, `AuditConfig.pdf_escalation_model`, `pdf_extraction_cache`

### Entries a misreading can explain are read again

- [ ] `p1` - **ID**: `cpt-referenceaudit-dod-pdf-input-llm-reread`

The system **MUST** re-read with the escalation model, once per document, every entry whose audit ends
in 'no match', a contradiction, or an error finding on its title, authors, year or DOI; **MUST** audit
the escalation model's reading in its place when the two readings differ, and keep the finding
otherwise; **MUST** state every re-read and its outcome on the entry; **MUST NOT** re-read an entry left
unresolved by a source failure; and **MUST** report a failed re-read on each entry it concerns.

**Implements**:
- `cpt-referenceaudit-algo-pdf-input-llm-escalation`

**Touches**:
- API: `pdf.llm_refs.counterpart`, `pdf.llm_refs.reading_differences`, `AuditPipeline._reread_flagged`
- Entities: `EntryAudit.issues`, `AuditReport.summary["pdf_reading"]`

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
- [x] A PDF built with the AI4X template is audited from its embedded records without GROBID, and
      `--no-network` gives its parse-only report; the records are exactly its cited entries, with the
      same content hashes as the `.bib` they were compiled from.
- [x] A record whose title is not in the printed reference list (a hand-edited `.bbl`) is flagged.
- [x] A malformed attachment falls back to GROBID with the reason in the input notes.
- [x] A PDF without embedded records is audited from a language-model reading, with no GROBID running;
      citedness is reported unknown.
- [x] A reading cut short (its last entry is not the one it named) or reporting that it stopped early
      is replaced by the escalation model's reading; when that is unusable too, the run fails with both
      reasons and no source is queried.
- [x] A DOI absent from the PDF's text is replaced by the escalation model's reading before any audit,
      and the absent DOI is never looked up.
- [x] An entry ending in 'no match' is re-read; read differently, the new reading is audited and
      reported with what changed; read the same, the finding stands. Matched entries are untouched,
      and one escalation reading serves every flagged entry.
- [x] A usable reading is served from the cache on the next run; an unusable one is not cached.
