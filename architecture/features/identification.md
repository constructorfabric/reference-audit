# Feature: Identification & Verdict

<!-- toc -->

- [1. Feature Context](#1-feature-context)
  - [1.1 Overview](#11-overview)
  - [1.2 Purpose](#12-purpose)
  - [1.3 Actors](#13-actors)
  - [1.4 References](#14-references)
- [2. Actor Flows (CDSL)](#2-actor-flows-cdsl)
  - [Audit one entry](#audit-one-entry)
- [3. Processes / Business Logic (CDSL)](#3-processes--business-logic-cdsl)
  - [Score and bucket a candidate](#score-and-bucket-a-candidate)
  - [Cluster accepted candidates into a verdict](#cluster-accepted-candidates-into-a-verdict)
- [4. States (CDSL)](#4-states-cdsl)
  - [Entry resolution status](#entry-resolution-status)
- [5. Definitions of Done](#5-definitions-of-done)
  - [Identify the artifact behind a reference](#identify-the-artifact-behind-a-reference)
  - [Three-way identification verdict](#three-way-identification-verdict)
  - [Hallucination screening](#hallucination-screening)
  - [Best version and canonical output](#best-version-and-canonical-output)
  - [Cache database and LLM calls](#cache-database-and-llm-calls)
- [6. Acceptance Criteria](#6-acceptance-criteria)

<!-- /toc -->

- [x] `p1` - **ID**: `cpt-referenceaudit-featstatus-identification`

## 1. Feature Context

- [x] `p1` - `cpt-referenceaudit-feature-identification`

### 1.1 Overview

The implemented networked audit slice layered on top of [Parsing](parsing.md): for each parsed
entry, query the bibliographic databases, score and cluster the candidates with the SAME-OBJECT
rule, and return the 3-way verdict plus the canonical best version — caching every successful
result so repeated runs are cheap.

### 1.2 Purpose

This feature realizes the identification, hallucination-screening, and best-version capabilities of
the audit pipeline. It is orchestrated by `pipeline.AuditPipeline._audit_entry_inner`, which routes
the entry to source adapters (`sources/`), scores candidates (`matching/features.py`,
`matching/scoring.py`), clusters them into distinct works (`matching/sameobject.py`), adjudicates
the residual ambiguity with the LLM (`llm/`, `matching/adjudicate.py`), and memoizes responses in
the SQLite cache (`cache/`). Each entry is audited in isolation; only successful results are cached.

**Requirements**: `cpt-referenceaudit-fr-identify-artifact`, `cpt-referenceaudit-fr-three-way-verdict`,
`cpt-referenceaudit-fr-hallucination-screen`, `cpt-referenceaudit-fr-best-version-canonical`,
`cpt-referenceaudit-nfr-cached-calls`

**Principles**: `cpt-referenceaudit-principle-modular-sources`,
`cpt-referenceaudit-principle-offline-first`

**Constraints**: `cpt-referenceaudit-constraint-id-preference`

### 1.3 Actors

| Actor | Role in Feature |
|-------|-----------------|
| `cpt-referenceaudit-actor-author` | Runs the full audit to obtain verdicts and the canonical best reference. |
| `cpt-referenceaudit-actor-reviewer` | Screens a submission's references for hallucinations (the `none` verdict). |
| `cpt-referenceaudit-actor-ai-agent` | Calls the audit programmatically while writing a paper. |
| `cpt-referenceaudit-actor-database` | The external bibliographic sources queried for candidates. |

### 1.4 References

- **PRD**: [PRD.md](../PRD.md)
- **Design**: [DESIGN.md](../DESIGN.md)
- **Dependencies**: `cpt-referenceaudit-feature-parsing`

## 2. Actor Flows (CDSL)

User-facing interaction: an actor requests a full audit of a reference and receives a 3-way verdict
plus, for an `exactly_one` match, the canonical best version.

**Use cases**: `cpt-referenceaudit-usecase-networked-audit`

### Audit one entry

- [x] `p1` - **ID**: `cpt-referenceaudit-flow-identification-audit-entry`

**Actor**: `cpt-referenceaudit-actor-database`

**Success Scenarios**:
- A real reference is matched to exactly one work and its canonical best version is reported.
- A fabricated reference returns a `none` verdict (hallucination screen).

**Error Scenarios**:
- A source/LLM failure leaves the entry `unresolved` (verdict `None`), never a false `none`; it is
  retried on the next run and never cached.

**Steps**:
1. [x] - `p1` - **IF** the whole-entry verdict is cached, reuse it and re-derive issues - `inst-cache-lookup`
2. [x] - `p1` - Route the entry to its id / metadata source adapters (`route_entry`) - `inst-route`
3. [x] - `p1` - API: query the routed sources for candidate records (concurrent, cached) - `inst-gather`
4. [x] - `p1` - **FOR EACH** pooled candidate compute features and bucket it (`_assess`) - `inst-assess`
5. [x] - `p1` - Cluster the accepted candidates and count them into a 3-way verdict - `inst-verdict`
6. [x] - `p1` - **IF** still unresolved **AND** an LLM is configured, run the per-candidate funnel - `inst-llm-adjudicate`
7. [x] - `p1` - Verify a URL-only `@misc` against its own cited page (`_resolve_web`) - `inst-web`
8. [x] - `p1` - Confirm a book's cited edition against Open Library (`_resolve_book`) - `inst-book`
9. [x] - `p1` - Backfill identifiers, note a better version, and enrich the canonical record - `inst-best-output`
10. [x] - `p1` - **IF** the verdict resolved, cache it (success only) - `inst-cache-store`

## 3. Processes / Business Logic (CDSL)

### Score and bucket a candidate

- [x] `p2` - **ID**: `cpt-referenceaudit-algo-identification-score`

Score one candidate record against the entry and assign it to the accept / reject / adjudicate
bucket. Called per candidate by the audit flow's `inst-assess` step.

**Input**: A parsed `BibEntry` and one candidate `SourceRecord`.

**Output**: A `CandidateAssessment` (feature vector + bucket).

**Steps**:
1. [x] - `p1` - Compute the full feature vector (`compute_features`) - `inst-features`
2. [x] - `p1` - **RETURN** the assessment with the formal bucket (`bucket`) - `inst-bucket`

### Cluster accepted candidates into a verdict

- [x] `p1` - **ID**: `cpt-referenceaudit-algo-identification-verdict`

Cluster the accepted candidates by the SAME-OBJECT rule into distinct works, then count them into
the 3-way verdict. Called by the audit flow's `inst-verdict` step.

**Input**: The entry's `CandidateAssessment`s and the `errored` flag.

**Output**: A `Verdict` (`none` / `exactly_one` / `multiple`) or `None` (unresolved).

**Steps**:
1. [x] - `p1` - Cluster the `auto_accept` candidates (SAME-OBJECT, `cluster_accepted`) - `inst-cluster`
2. [x] - `p1` - **RETURN** the counted 3-way verdict (`build_verdict`) - `inst-build`

## 4. States (CDSL)

### Entry resolution status

Each `EntryAudit` carries a `verdict` that is `None` (unresolved) until the audit flow settles it to
a `Verdict` of kind `none`, `exactly_one`, or `multiple`. The lifecycle is documented here for
completeness; it is not modeled as a standalone, traced state machine in this slice. The crucial
invariant is that a source/LLM failure leaves the status `unresolved`, never `none` — an error is
not a "not found".

**States**: UNRESOLVED, NONE, EXACTLY_ONE, MULTIPLE

**Initial State**: UNRESOLVED

**Transition**: UNRESOLVED → {NONE, EXACTLY_ONE, MULTIPLE} when the flow produces a `Verdict`;
UNRESOLVED is retained (and retried next run) on any transient failure.

## 5. Definitions of Done

### Identify the artifact behind a reference

- [x] `p1` - **ID**: `cpt-referenceaudit-dod-identification-identify-artifact`

The system **MUST** query the routed bibliographic sources for each entry and return the matching
artifact(s), preferring strong identifiers (DOI / ISBN / arXiv / OpenAlex Work id / Google Books
volume id) over metadata search. Books are additionally queried against **Google Books**, whose
forgiving title/author/ISBN search recovers real books that Open Library's strict title match (a
subtitle-bearing title, or a single off-by-one ISBN) reports as not found.

A cited OpenAlex Work id (an `openalex.org/W…` URL) is routed to OpenAlex's by-id lookup and treated
as authoritative identity: when the resolved Work matches the entry's title+author it is pinned as
the matched artifact (`_apply_openalex_identity`), so the article-centric pooler cannot dissolve the
explicitly-cited Work into a similar-titled foreign-DOI record and backfill the wrong identifiers. A
cited Work id whose Work has a mismatched title/author does not confirm the entry.

A cited **Google Books volume id** (a `books.google.…/books?id=…` URL) is handled the same way by
`_apply_google_books_identity`: the resolved volume, when it matches the entry's title+author, is
pinned as the matched artifact. This specifically prevents a same-titled journal-article (a book
*review* that reuses the book's title+authors and carries a DOI the book lacks) from being matched
and having its DOI backfilled onto the `@book`. Both identity overrides are implemented and covered
by tests but are not yet separately `@cpt`-traced flow instructions (they run alongside the traced
`inst-web` / `inst-book` identity steps); instruction-level tracing is planned.

**Implements**:
- `cpt-referenceaudit-flow-identification-audit-entry`

**Constraints**: `cpt-referenceaudit-constraint-id-preference`

**Touches**:
- API: `reference_audit.pipeline.run_audit(...)` / `AuditPipeline.run(...)`
- Entities: `SourceRecord`, `CandidateAssessment`, `MatchedArtifact`

### Three-way identification verdict

- [x] `p1` - **ID**: `cpt-referenceaudit-dod-identification-three-way-verdict`

The system **MUST** return exactly one of `none`, `exactly_one`, or `multiple` per resolved
reference, or leave it `unresolved` on transient failure.

**Implements**:
- `cpt-referenceaudit-algo-identification-verdict`

**Touches**:
- Entities: `Verdict`, `MatchedArtifact`

### Hallucination screening

- [x] `p1` - **ID**: `cpt-referenceaudit-dod-identification-hallucination-screen`

The system **MUST** return `none` for a reference no source matches, and **MUST NOT** return `none`
when a source errored (that entry stays `unresolved`).

For a **book**, Open Library is the authority of record for identity, so a `none` from the
article-centric matcher is only trustworthy when that authority was actually consulted. When the
Open Library edition lookup failed (transport/HTTP error), `_apply_book_identity` downgrades a `none`
verdict to `unresolved` (reported, retried next run) rather than assert a hallucination that was
never really checked. This override runs in the `inst-book` step and is covered by tests but is not
yet a separately `@cpt`-traced flow instruction.

**Implements**:
- `cpt-referenceaudit-algo-identification-verdict`

**Touches**:
- Entities: `Verdict`, `CandidateAssessment`

### Best version and canonical output

- [x] `p2` - **ID**: `cpt-referenceaudit-dod-identification-best-version`

The system **MUST** report a better version of a matched work (published over preprint, later book
editions) when one exists.

**Implements**:
- `cpt-referenceaudit-flow-identification-audit-entry`

**Touches**:
- API: `reference_audit.versioning.better_version_notes`
- Entities: `MatchedArtifact`, `EntryAudit`

### Cache database and LLM calls

- [x] `p2` - **ID**: `cpt-referenceaudit-dod-identification-caching`

The system **MUST** memoize successful whole-entry verdicts (and the underlying source / LLM calls)
so repeated audits reuse them, and **MUST NOT** cache transient errors.

**Implements**:
- `cpt-referenceaudit-flow-identification-audit-entry`

**Touches**:
- API: `reference_audit.cache.store.AuditCache.put_entry_verdict`
- Data: `cpt-referenceaudit-dbtable-response-cache`

## 6. Acceptance Criteria

- [x] A real reference is matched and reported with an `exactly_one` verdict.
- [x] A fabricated reference with no real match returns a `none` verdict.
- [x] A source/LLM failure leaves the entry `unresolved`, never `none`, and is not cached.
- [x] Repeated audits of the same inputs reuse the SQLite cache instead of re-querying.
- [x] A preprint with a published version (or a book with a later edition) reports the better version.
