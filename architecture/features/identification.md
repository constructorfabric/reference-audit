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
- **External benchmark**: [HALLMARK](https://github.com/rpatrik96/hallmark), driven by
  `benchmarks/hallmark_bench.py` (see README, "Benchmarking on HALLMARK")

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
- An unresolved entry records why, one line per cause, on `EntryAudit.unresolved_reasons` and as an
  `unresolved: …` issue: each failed source query (named by source and query kind), each failed LLM
  call, an Open Library outage for a book, candidates left undecided because the LLM is off, a
  low-confidence LLM ruling, or candidates beyond the per-entry LLM cap. Implemented in
  `_record_unresolved`, within the `inst-cache-store` step; not separately traced.

**Steps**:
1. [x] - `p1` - **IF** the whole-entry verdict is cached, reuse it and re-derive issues - `inst-cache-lookup`
2. [x] - `p1` - Route the entry to its id / metadata source adapters (`route_entry`) - `inst-route`
3. [x] - `p1` - API: query the routed sources for candidate records (concurrent, cached) - `inst-gather`
4. [x] - `p1` - **FOR EACH** pooled candidate compute features and bucket it (`_assess`) - `inst-assess`
5. [x] - `p1` - Cluster the accepted candidates and count them into a 3-way verdict - `inst-verdict`
6. [x] - `p1` - **IF** still unresolved **AND** an LLM is configured, run the per-candidate funnel - `inst-llm-adjudicate`
7. [x] - `p1` - Verify a URL-only `@misc` against its own cited page (`_resolve_web`) - `inst-web`
   (the fetcher detects a JavaScript single-page-app shell and re-fetches it via a headless browser
   before judging; a shell that cannot be rendered — no browser, or empty after rendering — is left
   unresolved, never read as a wrong/`none` URL. Implemented within this step; not separately traced.)
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

The `id_agreement` feature is **set-aware for ISBNs**: a book registers several ISBN-13s (print +
electronic, per edition), so each `Identifiers` carries the whole set (`all_isbn13()`). Two records
agree when their ISBN sets intersect, and `conflict` only when both carry ISBNs that are wholly
disjoint — a cite giving a volume's electronic ISBN no longer reads as a conflict against a source
record that lists the print ISBN. This is what lets a book-chapter cited by its containing volume's
ISBN reach an `auto_accept` (id-match) bucket deterministically, instead of every candidate being
forced to LLM adjudication.

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

Articles and conference papers are additionally queried against **DBLP**, the authority for the
premier CS/ML venues (NeurIPS, ICLR, ICML/PMLR, TMLR). These mint **no DOI** and are
thinly/ambiguously covered by the article-centric aggregators, so a real paper cited only by its
proceedings or OpenReview URL would otherwise be "unable to verify". For scoring, a bare URL is
**not** a strong anchor (`Identifiers.has_strong_id` excludes it — no feature compares a URL), so a
URL-only entry takes the strict title+author backfill path: a DBLP record with the exact title, full
author list, and year confirms it deterministically (no LLM required).

DBLP is read through its **SPARQL endpoint** (`sparql.dblp.org`). Its search API and mirrors now
answer automated clients with a bot-challenge HTML page, which made every DBLP query an error and so
left every unmatched entry unresolved. Each lookup makes two requests:
1. A word search over titles: every searchable word of the cited title must match, ASCII words only,
   with LaTeX math dropped. Results come shortest title first, so the exact title ranks ahead of
   longer titles that quote it.
2. A lookup of those publications' properties and ordered author signatures.

A non-JSON page, an error status, or a title with no searchable word is a source **error**, never
"not found".

Semantic Scholar, OpenAlex and DBLP have a second **source backend**, selected per run
(`source_backend`: `api` by default, or `clickhouse`). It is a local ClickHouse mirror of the same three
databases (`sources/clickhouse.py`).
- **Interchangeable:** the local adapters keep the API adapters' names, and pass their rows through
  the API normalizers wherever one exists, so routing, field priorities, identity pinning and reports
  are unchanged.
- **Title search:** a full-text title index with every searchable word required, shortest title
  first, then most-cited (the DBLP word-search contract). When no such hit is a near-exact title
  (`title_ratio` below `title_accept`) and the title has at least 4 distinct words, the search is
  repeated with each word left out in turn (one query, served by the same index), ranked by edit
  distance to the cited title. One misspelt or extra word then no longer hides the real record.
- **Preflight:** before any entry is audited, the run checks that the server answers and that every
  title index exists and is fully built. Otherwise it stops with the statements that build them,
  never degrading into per-entry scans.
- **Failures:** a query failure or timeout is a source **error**.
- **Caching:** cached source responses are keyed per backend (`cache_source`), and the verdict cache
  is keyed by `(entry_hash, backend)`, so one backend's snapshot never answers for the other's.
- **Coverage:** the preflight also reads how far each snapshot reaches (`coverage_end`: DBLP's dump
  date, OpenAlex's newest `updated_date`, the S2 dump's newest publication date). A `none` verdict
  for an entry dated in or after a snapshot's year carries an issue naming that snapshot, since a
  newer work cannot be in it. The verdict itself stands: the other sources are live.
- **Known limits:** coverage ends at each snapshot's ingest date, OpenAlex has no `locations`
  locally (version links are the primary and best open-access locations only), and the DBLP dump has
  no landing page or pages.

This backend, the leave-one-out search and the coverage caveat are implemented and unit-tested
(`tests/test_clickhouse.py`, `tests/test_unresolved.py`, plus a live check gated on
`REFERENCE_AUDIT_LIVE`). They are not yet `@cpt`-traced to their own flow or algorithm.

A **truncated author list** — the BibTeX `and others` convention (and a written-out "et al.") —
is treated as a truncation marker, not a literal author (`matching/names.py`). Left in, the phantom
surname "others" would drag author overlap down and break the subset check (an intentionally
abbreviated list is no longer ⊆ the full author list), tripping the distinct-author-set veto and
forcing needless adjudication — which, with no LLM, leaves an otherwise-confirmable paper (e.g. a
~30-author RLHF survey) unresolved. The marker is dropped, so the named authors matching a prefix of
the record's full list confirms identity. The author field check (below) reads the marker the same
way: a list that says it is shortened is never reported as a partial author list.

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

A book cited by a **chapter-level DOI** (`10.1093/{isbn}.003.0002`) resolves to a *component* whose
title differs from the book, so the article-centric matcher returns `none`. When the entry carries no
ISBN of its own, `_book_backfill_isbns` takes the ISBNs from that cited DOI's own record (trusted
because the published DOI matches the entry's, or because the record is an Open Library edition during
cached re-derivation — never a mere same-author record) and `_resolve_book` queries Open Library by
every one of them (the adapter's `_work_keys` tries each ISBN, since a book registers several and
Open Library indexes only some). The cited *edition* is still selected by the entry's own
year/publisher, so the match grounds on the edition the author cited, not whichever reprint the DOI
rides on. The editions fetch is cache-keyed by the original entry so the cached re-derivation reports
identically. This runs in the `inst-book` step.

A **cited DOI** that the matched work does not carry is checked on every `exactly_one` match
(`_check_cited_doi`, run with the field checks). Without it, an entry whose title and authors match
a real work passed clean even when its DOI pointed at another paper or at nothing, which is the
`fabricated_doi` pattern. The result is a `FieldFinding` on the `doi` field:
- `ok` when the matched work carries the DOI. Pooling keeps every member record's DOI
  (`raw["merged_dois"]`), since the pooled record's own `ids.doi` is one DOI only, often the arXiv
  DataCite DOI of the preprint.
- `error` when a source's by-id record for the DOI is a different work (named in the finding), or
  when the doi.org Handle API does not know the DOI (`responseCode` 100). The resolver itself is not
  asked: for a DOI under a prefix nobody registered (`10.8888/...`) it answers HTTP 500, not 404, which
  read as an outage and left HALLMARK's fabricated DOIs `unverifiable`.
- `uncertain` when that record has the same title and authors but was not merged, or when doi.org
  knows the DOI but no source has a record for it.
- `unverifiable` when no source has a record and doi.org cannot be reached.

Books (identified by Open Library editions, and legitimately cited by a chapter-level DOI) and arXiv
DataCite DOIs are not checked. The DOI's own lookup is cached under a probe entry keyed by that DOI.
The finding is advisory like every field finding: the verdict is unchanged. Implemented and tested
(`tests/test_cited_doi.py`, `tests/test_publisher.py`); not separately `@cpt`-traced.

**Pooling keeps distinct works apart and keeps its members** (`matching/pool.py`, pipeline 0.22).
- A version relation joins two groups of records only when every record of one has authors
  compatible with every record of the other. The relation is a matching title and author list, or a
  database's version link, which comes from the aggregators' own work-merging and can be wrong.
  Authors are compared person by person (`names.authors_compatible`: at least 80% of the shorter list
  are on the longer one).
  - Before, the fuzzy surname overlap rated CrossFormer's authors 0.85 against CrossFormer++'s, with
    two of seven people different.
  - A one-author supplementary-material DOI fitted both lists and bridged them.
  - So a conference paper was fused with its journal extension, and the matching ICLR record
    disappeared behind the extension's metadata (a `none` for a real paper).
- A shared identifier still merges unconditionally.
- A pooled record keeps its member records (`SourceRecord.members`, each without `raw`, abstract or
  members of its own).
- Its venue is never a preprint server or repository (`features.is_repository_venue`: arXiv, DBLP's
  `CoRR`, Infoscience, …) while a member names the journal or conference. DBLP joins the venue
  priority after OpenAlex.

**Field checks compare against the version the entry cites** (`fieldcheck._ordered_records`).
- The fields are compared against the pooled record's members, in this order:
  - those of the kind the entry cites: a preprint when its venue is a preprint server, or when it
    has no venue but an arXiv id; otherwise the journal/conference version;
  - then those from the cited year;
  - then by source authority.
- Before, the pooled record's compiled fields stood in for every version. An ICLR paper was compared
  with OpenAlex's arXiv copy: venue `unverifiable` on 168 of 513 HALLMARK VALID entries, and the
  preprint's year and title reported as wrong. A conference paper was compared with its later
  journal version.
- The LLM tie-break is shown the matched work as the database records it. Before, it was shown the
  entry under review, labelled "the same work, confirmed by identifier", so it judged a different
  title to be the same.
- A substituted content word in a title is an `error`.

**Authors are a field finding** (`field = "author"`, `fieldcheck._author_check`).
- Each cited author is compared person by person (`names.same_person`) with every source's author
  list. The comparison is order-free (`Tian Li` / `Li Tian`) and allows a second surname, initials,
  hyphens and umlaut transliteration. A near-namesake fails (`Carreira` / `Barreira`).
- A cited author found on no source is an `error`.
- When every record is shorter than the citation and is, in order, its leading part, the authors past
  its end are `unverifiable`, since the record may have been cut there.
- A citation that names only some of the work's authors without `and others` is reported per
  `AuditConfig.partial_authors` (CLI `--partial-authors`): `ignore`, `warn` (`uncertain`, the
  default) or `error`. Many bibliographies shorten long lists; HALLMARK counts an unmarked omission as
  a hallucination.
- This replaces the earlier free-text issue ("author … not found in … record"). That issue
  compared surnames fuzzily against the best record only, and skipped the check whenever the record
  was shorter than the citation, so a wholesale-replaced author list passed.

These changes are implemented and tested (`tests/test_pool.py`, `tests/test_fieldcheck.py`,
`tests/test_names.py`); they are not separately `@cpt`-traced.

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

A by-id lookup is cached under the entry only when it uses the entry's own identifiers. Enrichment by
a matched or backfilled identifier is cached under a probe entry keyed by those identifiers;
otherwise it overwrote the entry's `id` slot, and the next run's identification read the matched
work's records as the entry's own by-id result.

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
- [x] Verdict accuracy is measured on the HALLMARK `dev_public` split under a fixed, documented
      verdict-to-label mapping (`benchmarks/hallmark_bench.py`). The harness lives outside
      `src/reference_audit` and is **not** `@cpt`-traced. A record the tool could not audit is
      reported as not evaluated, never given a guessed label. Measured at pipeline 0.21; the
      result and the gaps it exposed are in README, "Benchmarking on HALLMARK".
