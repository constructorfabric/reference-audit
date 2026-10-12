# reference-audit

Audit the references in a paper. Given a `.bib` bibliography and the `.tex` that cites it — or just
the paper's **PDF** — `reference-audit` figures out **which real-world document each reference
actually points to** and flags ones that don't match anything real (hallucinated references).

For every entry it returns one of three verdicts:

- **exactly one match** — the reference resolves to a single real document (with its DOI/ISBN/URL,
  and any preprint↔published versions merged).
- **no match** — no real document corresponds to the entry (a likely hallucination).
- **multiple matches** — the entry is ambiguous and matches more than one distinct document.

It also backfills missing DOIs/ISBNs, normalizes malformed identifiers, and reports dangling or
uncited citations.

> Designed for three audiences: authors polishing their bibliography, automated screening of
> submissions for hallucinated references, and AI agents writing papers. The full product
> specification lives in [`architecture/SPEC.md`](architecture/SPEC.md).

## How it works

For each entry the tool runs a funnel that prefers cheap, deterministic evidence and only escalates
to an LLM when needed:

1. **Parse** the input — either the `.bib` (resolving `\cite`/`\nocite` in the `.tex`), or a PDF, from
   which a locally-run [GROBID](https://github.com/grobidOrg/grobid) extracts both the printed
   reference list and the in-text citation markers. A PDF that embeds the BibTeX records of its cited
   works (as one made with the [AI4X template](#pdfs-that-embed-their-references) does) is read from
   those records instead, without GROBID. Either way, DOIs, ISBNs, arXiv ids, OpenAlex Work
   ids (an `openalex.org/W…` URL becomes a first-class identifier) and Google Books volume ids (a
   `books.google.…/books?id=…` URL) are normalized identically, and everything below is unchanged.
   HTML character references left by web-scraped metadata (`d&apos;Amore`, `&amp;`) are decoded in
   the `.bib`'s title, author, venue, publisher and pages, and in every source record's title,
   authors, venue and publisher, so the two compare as plain text in identification and in the field
   check of step 7, and findings report the decoded value.
2. **Query** multiple scholarly databases — Crossref, OpenAlex, Semantic Scholar, arXiv, DBLP, Open
   Library, Google Books — by identifier where the source supports it, and by title/author. The
   title/author search is routed by entry type: a `@book`/`@incollection` goes to Open Library,
   Google Books and Crossref; a `@misc` also to arXiv; anything else to Crossref, OpenAlex, Semantic
   Scholar and DBLP. With the [ClickHouse backend](#local-clickhouse-backend), Semantic Scholar,
   OpenAlex and DBLP are read from a local mirror instead. A cited Google Books volume id is
   resolved directly to that volume, and a cited OpenAlex Work id to that Work when the entry
   carries no DOI or arXiv id (OpenAlex is queried by those first) — the authoritative key for
   entries, notably trade books, the other sources miss. DBLP is the authority for the premier
   CS/ML venues (NeurIPS, ICLR, ICML/PMLR, TMLR), which mint no DOI and are thinly covered by the
   article-centric sources. A paper cited only by its proceedings or OpenReview URL is confirmed by
   a near-exact title and a strong author overlap with a record (a bare URL is not treated as a
   matching anchor, so such an entry takes the same strict title+author path as one with no
   identifier at all). DBLP is queried through its SPARQL endpoint (`sparql.dblp.org`), because its
   search API now serves automated clients a bot-challenge page, never JSON. A truncated author
   list (the BibTeX `and others` / "et al." convention) is recognized as such, so its named authors
   all appearing in the full record's author list (in any order) confirms the entry rather than
   reading the omitted names as a different work. The **publisher** of record (the DOI landing
   page's BibTeX export, for MIT Press / Silverchair and SAGE / Atypon) is not an identity source: it
   is queried only after the verdict, to enrich the matched work's venue, volume, issue, pages,
   publisher and year.
3. **Pool** the results, merging records that are the same work (shared identifier, or a
   preprint↔published version link). Pooling keys on a record's canonical ISBN-13 only, so a book
   chapter that inherits its volume's ISBNs is not fused with its sibling chapters. When the
   accepted candidates are counted as works, ISBNs are treated as a *set*: one book registers several
   ISBN-13s (print + electronic, per edition), so records that share any one of them are the same
   work rather than different books. A version relation (a matching title, or a
   database's version link) joins two groups only when every record of one has authors compatible
   with every record of the other, compared person by person. So a conference paper is not fused with
   a journal extension that has other authors (CrossFormer at ICLR, CrossFormer++ in TPAMI), not even
   through a one-author supplementary-material DOI that fits both. A pooled record keeps its member
   records, and its venue is never a preprint server (`arXiv`, DBLP's `CoRR`) while a member names
   the journal or conference. Its author list, which scoring and the LLM see, comes from the most
   reliable source that has one (the publisher, then DBLP, Crossref, arXiv, OpenAlex, Semantic
   Scholar), not from the citation-richest record.
4. **Score** each candidate with interpretable features (title/author/year/venue similarity,
   identifier agreement, and distinct-work signals). Identifier agreement is likewise set-aware for
   ISBNs — a cite that gives a book's electronic ISBN matches a source record carrying that book's
   print ISBN, and it counts as a *conflict* only when the two ISBN sets are wholly disjoint (this is
   what lets a book-chapter cited by its volume's ISBN resolve deterministically instead of falling
   to the LLM).
5. **Adjudicate** anything the formal rules leave undecided with an LLM, asking one record at a time
   whether it can correspond to the entry; a second LLM check decides whether two strong candidates
   are the *same* work. At most `llm_max_candidates` (8) candidates, the best-scoring, are
   adjudicated per entry; an entry with more is left unresolved and says so.
6. **Verdict** — count the distinct works and report none / exactly one / multiple. An entry left
   without a verdict says why: the source that failed, the LLM call that failed, or the candidates
   that stayed undecided.
7. **Check the fields** of an exactly-one match (title, authors, venue, year, volume, pages, …).
   - Each field is compared against the **version the entry cites**, read from the pooled record's
     members: a citation of the arXiv preprint against the preprint, one of a journal or conference
     against the published paper, and of a conference paper and its later journal version, the one
     from the cited year. Within that version the value most sources agree on wins, so one source's
     defect (OpenAlex's corrupted title for Constitutional AI) is outvoted.
   - **Authors** are checked person by person against every source's author list, so name order
     (`Tian Li` / `Li Tian`), compound surnames, diminutives (`Tim` / `Timothy`) and one source's
     defect do not flag a real author, and a near-namesake does not pass (`Carreira` is not
     `Barreira`). A cited author on no source is an
     `error`. A citation that names only some of the work's authors without `and others` is reported
     per `--partial-authors`: `ignore`, `warn` (an `uncertain` finding, the default) or `error`.
   - A difference no rule settles goes to the LLM, which is shown the matched work as the database
     records it, never the entry under review. A substituted title word, or a different journal or
     conference series, is an error.
   - The **cited DOI** is checked too (except for a book, which is legitimately cited by a chapter
     DOI, and an arXiv DataCite DOI): when the matched work does not carry it, the DOI's own records
     are looked up. A DOI that belongs to a different paper, or that the doi.org Handle API does not
     know, is reported as a wrong `doi` field, naming that paper. A DOI found for backfill is checked
     at doi.org too: one that does not resolve is reported as unusable, not as a find.
8. **Suggest a better version** of a matched work: the published version of a cited preprint, a
   newer arXiv version, or a later edition of a cited book.

A reference identified only by a URL (a `@misc` blog post, software or project page that no scholarly
database indexes) is verified against the page itself: the tool fetches the URL, checks the page's
own HTML metadata (Open Graph / Twitter card / `citation_*` / Dublin Core / `<title>` + authors)
against the citation, and falls back to the LLM when that metadata is missing or inconclusive. A dead
link or a page that cannot be confirmed is reported and left unresolved, never silently passed. Only
a high-confidence LLM judgment that the live page is a *different* resource gives *no match* (a
possibly wrong or fabricated URL).

Many such pages are **JavaScript single-page apps** (e.g. `data.europa.eu` dataset pages): a plain
HTTP fetch returns only an empty app shell, with the real title/text injected after the page's
JavaScript runs. The tool detects these shells and re-fetches them through a headless browser
(Chromium via `--dump-dom`) so the rendered page is judged like any other. This is the reliability
fix for a class of false hallucinations: an unreadable shell is *never* read as "a different page".
If no headless browser is available, or rendering fails, the entry is reported and left unresolved
(retried next run) rather than flagged. Rendering is on by default (`web_render_enabled`); point
`WEB_RENDER_BROWSER_PATH` at a Chrome/Chromium binary if one is not auto-detected on `PATH`.

A reference that cites an **OpenAlex Work id** (an `openalex.org/W…` URL, common for books and other
titles the article-centric search and Crossref/Open Library miss) is resolved directly to that Work,
unless the entry also carries a DOI or arXiv id, by which OpenAlex is then queried instead.
When the Work matches the entry's title and authors it is pinned as the match — the author-supplied
Work id is treated as authoritative identity, so a similar-titled foreign-DOI record is never merged
in and its identifiers are never backfilled onto the entry. A cited Work id whose Work has a
mismatched title/author does not confirm the entry; the entry gets whatever verdict the other
candidates support, with no finding specific to the Work id.

For **books**, Open Library is the authority of record for identity: it is edition-aware, so it can
confirm a real book the article-centric matcher rejected (e.g. a 1976 original whose only DOI-bearing
candidate is a 2018 reprint). Because of this, a book is only ever called a hallucination when that
authority was actually consulted — if Open Library is unreachable, an article-search "no match" is
**not** trusted: the entry is left unresolved (reported, retried next run) rather than flagged as a
likely hallucination we never really checked.

A book is frequently cited by a **chapter-level DOI** (Oxford Scholarship Online and similar mint one
DOI per chapter, e.g. `10.1093/{isbn}.003.0002`), which resolves to a *component* whose title differs
from the book — so the article-centric matcher rejects it as a non-match. When such a book carries no
ISBN of its own, the ISBNs on that cited DOI's own record are used to locate the book in Open Library
(every ISBN is tried, since one book registers several and Open Library indexes only some). The cited
*edition* is still pinned by the entry's own year/publisher, so a book cited by a chapter DOI grounds
on the edition the author actually cited (the 1989 original, not a 1992 reprint the DOI rides on), and
the report notes that a chapter/component DOI was cited.

**Google Books** supplements this where Open Library falls short. Open Library's title search is
strict — a title carrying its subtitle (*"Why Nations Fail: The Origins of Power, Prosperity, and
Poverty"*) or a single off-by-one ISBN can return nothing, so a real book is reported "not found
there". Google Books is more forgiving (`intitle:`/`inauthor:`/`isbn:`), and when the `.bib` carries
a Google Books **volume id** in its URL, that volume is resolved directly and pinned as authoritative
identity — exactly like a cited OpenAlex Work id. This matters because a same-titled journal-article
(e.g. a book *review* that reuses the book's title and authors and carries a DOI the book lacks) would
otherwise be matched and have its DOI wrongly backfilled onto the `@book`. For a `@book` or
`@incollection`, an edition Open Library confirms still takes precedence over either pin.

Results are cached in a local SQLite DB, so re-running on the same `.bib` makes **no** repeat
network or LLM calls for what succeeded. Errors are never cached: an entry left without a verdict,
a failed source query or an undetermined doi.org check is retried on the next run. A transient
outage never counts as "no match".

## Requirements

- Python 3.14
- [`uv`](https://docs.astral.sh/uv/)
- An OpenAI API key (for the LLM adjudication step)
- **For PDF input only:** a running GROBID. It is not managed by this tool — start one with:
  ```bash
  podman run -d --replace --name grobid -p 8070:8070 docker.io/grobid/grobid:0.9.1-crf
  ```
  The first start takes ~30 s while the models load. `--replace` swaps out an existing `grobid`
  container (e.g. one from an older image). Docker has no `--replace`: run `docker rm -f grobid`
  first, then the same command with `docker` instead of `podman` and without `--replace`.

## Setup

```bash
uv sync
```

Create a `.env` file with your keys in the directory you run the tool from (normally the project
root; it is read from the current directory, and is git-ignored):

```dotenv
# Required for LLM adjudication
OPENAI_API_KEY=sk-...
# OPENAI_BASE_URL=...               # optional — an OpenAI-compatible endpoint
# Optional — sampling temperature. Unset ⇒ the model default (gpt-6-luna accepts only its default);
# set LLM_TEMPERATURE=0 when overriding --model with one that supports it, for more stable verdicts
# LLM_TEMPERATURE=0

# Optional — improve coverage / rate limits for the data sources
S2_API_KEY=...
CROSSREF_MAILTO=you@example.org     # Crossref/OpenAlex polite-pool contact
PAPER_SEARCH_MCP_UNPAYWALL_EMAIL=you@example.org   # used as that contact when CROSSREF_MAILTO is unset
OPENLIBRARY_EMAIL=you@example.org   # sent in the User-Agent on Open Library requests (polite identification)
GOOGLE_BOOKS_API_KEY=...            # Google Books per-project quota (the keyless endpoint shares a global daily quota that is routinely exhausted)

# Optional — only used for PDF input; overridable per-run with --grobid
GROBID_URL=http://localhost:8070

# Optional — read Semantic Scholar, OpenAlex and DBLP from a local ClickHouse mirror instead of their
# APIs (see "Local ClickHouse backend"); overridable per-run with --backend
SOURCE_BACKEND=clickhouse          # default: api
CLICKHOUSE_HOST=127.0.0.1
CLICKHOUSE_PORT=8123
CLICKHOUSE_USER=default
CLICKHOUSE_DEFAULT_USER_PASSWORD=...   # or CLICKHOUSE_PASSWORD
# CLICKHOUSE_S2_DB=s2ag  CLICKHOUSE_OPENALEX_DB=openalex  CLICKHOUSE_DBLP_DB=kb   (the defaults;
#   CLICKHOUSE_DB is accepted for CLICKHOUSE_S2_DB)

# Optional — a Chrome/Chromium binary for rendering JavaScript pages, if none is on PATH
# WEB_RENDER_BROWSER_PATH=/usr/bin/chromium
```

`NCBI_API_KEY`, `NASA_ADS_API_KEY` and `CORE_API_KEY` are accepted in `.env` but not used: there is
no PubMed, ADS or CORE source. Any other `AuditConfig` field (in `config.py`) can also be set by its
upper-cased name, e.g. `MODEL=` replaces the default model and `USE_LLM=false` turns the LLM off.

Only `OPENAI_API_KEY` is needed to run the full pipeline; the data sources used by default
(Crossref, OpenAlex, Semantic Scholar, arXiv, DBLP, Open Library, and the publisher and cited-page
fetches) require no key. Google Books works without a key but on a
shared global daily quota that is frequently exhausted — set `GOOGLE_BOOKS_API_KEY` for reliable
book coverage at scale. You can also run with no LLM at all (`--no-llm`, see below).

## Usage

Two input forms — a manuscript with its bibliography, or a PDF on its own:

```bash
uv run reference-audit audit <main.tex> <references.bib> [options]
uv run reference-audit audit <paper.pdf> [options]
```

They are mutually exclusive: a PDF *is* the whole input, since GROBID extracts both its reference list
and its in-text citations (or the PDF embeds its references' BibTeX records), so passing a `.bib`
alongside one is rejected.

Example, against the bundled pilot:

```bash
uv run reference-audit audit \
  tests/documents/directing-open-ended-evolution/initial.tex \
  tests/documents/directing-open-ended-evolution/initial.bib
```

```
Reference audit
  28 entries  ·  28 cited  ·  0 uncited  ·  7 with issues  ·  1 commented twins
  verdicts: 28 matched  ·  0 no-match  ·  0 ambiguous  ·  0 unresolved
  types: article=18, book=2, inproceedings=5, misc=3

CAPITAL OFFENCES — No hallucinated citations

UNABLE TO VERIFY — For all other references at least one matching artifact was positively identified

ISSUES (7) — other problems to review:

[article] wolpert2007  (cited)
    Using self-dissimilarity to quantify complexity
    ids: doi:10.1002/cplx.20165
    ⚠ DOI normalized from URL form ('https://doi.org/10.1002/cplx.20165' → '10.1002/cplx.20165')
    ✓ exactly one match (high) — Matched a single work via crossref.
      matched: doi:10.1002/cplx.20165  (crossref)

[inproceedings] fu2023dreamsim  (cited)
    DreamSim: Learning New Dimensions of Human Visual Similarity using Synthetic Data
    ids: (no identifier)
    ⚠ no DOI/arXiv id (will attempt DOI backfill)
    ✓ exactly one match (high) — Matched a single work via semantic_scholar (2 versions).
...

FORMATTING NITS & ADVISORIES (3) — cosmetic field fixes or citation notes:

[article] goldenfeld1992lectures  (cited)
    Lectures on Phase Transitions and the Renormalization Group
    ids: doi:10.1201/9780429493492
    · formatting nit in 'pages'='185-–197' — non-standard page separator in '185-–197'; use '--' [crossref]
    ✓ exactly one match (high) — Matched a single work via crossref.
      matched: doi:10.1201/9780429493492  (crossref)
...

NO ISSUES (18) — verified, nothing to fix:
...
```

### Options

| Option | Description |
| --- | --- |
| `-f, --format text\|json\|both` | Output format. `json` emits the full structured report (verdicts, candidates, features) — ideal for tooling and AI agents. Default: `text`. |
| `--no-network` | Parse only: identifiers + cited/uncited bookkeeping, no database or LLM calls. |
| `--no-llm` | Formal-only: skip LLM adjudication for fully deterministic output (useful in CI). |
| `--check-citations` | Advisory: for each in-text citation of a matched reference, check the citing context against the cited work's **abstract** (needs the LLM). See [Citation alignment](#citation-alignment---check-citations). |
| `--fresh` | Empty the cache DB, then re-query everything. This deletes every cached result in that DB, including other bibliographies' if they share it. |
| `--cache PATH` | Cache DB location. Default: `.reference_audit/cache.db` beside the `.bib` (or the `.pdf`). |
| `--model NAME` | Override the LLM model (default `gpt-6-luna`). |
| `--grobid URL` | GROBID base URL for PDF input. Default `http://localhost:8070` (or `GROBID_URL`). |
| `--ignore-embedded` | For a PDF that embeds the BibTeX records of its cited works, extract the references with GROBID anyway. See [PDFs that embed their references](#pdfs-that-embed-their-references). |
| `--backend api\|clickhouse` | Where Semantic Scholar, OpenAlex and DBLP are read from: their public APIs, or a local ClickHouse mirror. Default `SOURCE_BACKEND`, else `api`. See [Local ClickHouse backend](#local-clickhouse-backend). |
| `--fail-on hallucinated\|multiple` | Exit non-zero if any entry gets that verdict — for gating submissions in CI. |
| `--partial-authors ignore\|warn\|error` | How to report a cited author list that names only some of the work's authors without `and others`: not at all, as `uncertain` (default), or as an `error`. Default `PARTIAL_AUTHORS`, else `warn`. |

`--no-network` applies to `.tex` + `.bib` input, and to a PDF that embeds the BibTeX records of its
references. Any other PDF's reference list comes from an HTTP call to GROBID, so there is no offline
parse path for it, and combining the two is rejected rather than quietly redefining what the flag
promises.

Example — fail a CI check if any reference looks hallucinated, as JSON:

```bash
uv run reference-audit audit paper.tex refs.bib --format json --fail-on hallucinated
```

### Local ClickHouse backend

Semantic Scholar, OpenAlex and DBLP can be read from a local [ClickHouse](https://clickhouse.com)
mirror instead of their public APIs: the S2 Academic Graph dump (database `s2ag`), an OpenAlex
snapshot (`openalex`) and the DBLP XML dump (`kb.dblp_publication`). Select it with
`SOURCE_BACKEND=clickhouse` in `.env` or `--backend clickhouse`. The other sources (Crossref, arXiv,
Open Library, Google Books, publisher and web fetches) always use their APIs. Those requests stay inside
each API's published limits: they are spaced per source, and Crossref (3 at a time, its polite-pool
limit) and arXiv (1) also cap requests in flight, retries included. arXiv's terms allow one request
every three seconds, so lookups by arXiv id are the slowest step of a large batch. Without these
limits, a batch of a few hundred entries turned about a fifth of them into 429s.

The local sources have no rate limits and no third-party outages. A large batch is then paced by the
API sources that remain: about 45 minutes per thousand references, mostly Crossref's three requests in
flight. On the Semantic Scholar API (1 request/s), the same batch takes about an hour.
The two backends are interchangeable to the rest of the pipeline. The local adapters keep the API
adapters' source names, and shape their rows like the API's JSON before the same normalizers. Their
cached responses are kept apart, and the verdict cache is keyed by backend, so neither backend ever
serves the other's result.

**Title search** needs a full-text index on each searched table. It matches titles that contain
every searchable word of the cited title, shortest title first, then (S2 and OpenAlex, which have
citation counts) most-cited first. When none of those is a near-exact title (and the title has at
least 4 distinct words), it searches again with each word left out in turn, ranked by edit distance
to the cited title. So one misspelt or extra word no longer hides the real paper; two still do.
Before any entry is audited, the run checks that the server answers, that each index exists and is
fully built, and that each snapshot reports a coverage date. If not, it exits rather than scanning
hundreds of millions of rows per reference: for a missing index, with the statements below; for an
index still building, asking you to wait for its `system.mutations` entry to finish.

```sql
ALTER TABLE s2ag.papers           ADD INDEX IF NOT EXISTS idx_title_text title TYPE text(tokenizer = splitByNonAlpha, preprocessor = lower(title));
ALTER TABLE openalex.works_slim   ADD INDEX IF NOT EXISTS idx_title_text title TYPE text(tokenizer = splitByNonAlpha, preprocessor = lower(title));
ALTER TABLE kb.dblp_publication   ADD INDEX IF NOT EXISTS idx_title_text title TYPE text(tokenizer = splitByNonAlpha, preprocessor = lower(title));
ALTER TABLE <each of the above>   MATERIALIZE INDEX idx_title_text;   -- runs in the background; see system.mutations
```

On the reference machine these built in about 6 minutes and take 7.1 GiB (S2), 13.6 GiB (OpenAlex)
and 181 MiB (DBLP). A title lookup then takes 0.1–2 s and an identifier lookup 20–90 ms. A
40-reference HALLMARK sample audits in about 30 s. Identifier
lookups use `s2ag.paper_external_ids` and `openalex.works_slim.doi`, which need no extra index.

What the local data cannot supply, compared with the APIs:

- **Coverage ends at the snapshot.** The run reads each snapshot's end at startup (DBLP's dump date,
  OpenAlex's newest `updated_date`, the newest publication date in the S2 dump) and prints it in the
  report header. On the reference machine: OpenAlex 2026-06-26, DBLP 2026-09-19, S2 current. A work
  newer than its snapshot is not there, so a *no match* for an entry dated in or after a snapshot's
  year carries a note naming the snapshot. Audit very recent papers with the API backend.
- **OpenAlex has no `locations` locally**, so a Work's version links are its primary and best
  open-access locations only. The preprint↔published merge then also rests on the identifier links
  the other sources supply. Pages and the abstract come from the full `openalex.works` table (on
  disk, about 1 s per batch).
- **The DBLP dump table has no landing-page URL and no pages.**
- Title search tolerates one wrong word (see above); the API searches are more forgiving still.
- A cold title search on S2 takes up to about 2 s: the text index is evaluated per part, and the
  largest part holds about 200 M rows. Accepted as is; a finer index granularity could help.

### Reading the output

The text report leads with two headline categories:

- **`CAPITAL OFFENCES`** — hallucinated citations: entries that conclusively resolve to **no** real
  document (verdict *no match*). When there are none, the report says so explicitly
  (*No hallucinated citations*).
- **`UNABLE TO VERIFY`** — entries we could **not** conclusively clear of being a hallucination, for
  any reason: a transient network/LLM error, an unfamiliar `.bib` type, a dead link, an
  adjudication left unsettled. This is the catch-all that keeps an inconclusive check from masquerading
  as a clean pass. When it is empty the report states that for every other reference *at least one
  matching artifact was positively identified*.

The remaining entries (at least one match found) are then split into `ISSUES`,
`FORMATTING NITS & ADVISORIES`, and `NO ISSUES`. Per-entry verdict glyphs:

- **`✓ exactly one match`** — resolved to a single document; the matched identifier and version
  count are shown.
- **`✗ NO MATCH (possible hallucination)`** — nothing real corresponds; treat as a likely
  hallucination.
- **`? MULTIPLE matches`** — ambiguous; the entry matches more than one distinct work.
- **unresolved** (no verdict line) — the tool could not conclude (e.g. a transient API error, or a
  cited web page that is a dead link, a JavaScript app shell no browser could render, or otherwise
  could not be confirmed). Never reported as a hallucination. Each cause is a `⚠` line: usually
  `⚠ unresolved: …`, naming the failed source or the undecided candidates; `⚠ audit failed …` when
  the entry's audit raised; `⚠ no title and no identifier …` when there was nothing to search on.
  The JSON carries the list as `unresolved_reasons`.
- **`⚠` lines** — per-entry issues: a normalized/backfilled identifier, a missing ISBN, a dangling
  citation, etc.
- The header counts cited and uncited entries; the **cited-but-missing** citations (a `\cite` with
  no `.bib` entry) and the **uncited** entries are listed at the end of the report.
- **`UNPARSEABLE .bib ENTRIES`** — entries that could not be read, with their line and the reason:
  an unbalanced brace (most often), another syntax error bibtexparser reports, a reference to an
  undefined `@string`, a duplicate field, no fields at all, or a value whose LaTeX cannot be decoded.
  They are not checked at all, and a `\cite` of one is not counted as missing. The JSON lists them
  under `unparsed`.

### Citation alignment (`--check-citations`)

A reference can be perfectly real yet cited for a claim its source never makes. With
`--check-citations`, for every in-text `\cite` of a reference that resolves to **exactly one** work
(other references get no alignment finding),
the auditor extracts the **citing context** (the sentence attaching the claim to the citation) and
compares it against that work's **abstract**, classifying each citation as:

- **`supported`** — the abstract corroborates the citing claim.
- **`contradicted`** — the abstract asserts the opposite; surfaced as a loud per-citation issue
  (*citation may misrepresent the source*), with the abstract quote.
- **`not_in_abstract`** — the abstract is silent on the claim. An abstract is only a summary, so this
  is **not** flagged as misuse — it is an advisory note (the full text may well support the claim).
- **`unverifiable`** — no abstract was retrievable, or the LLM was unavailable or its call failed.
  Reported, never guessed — a silent/absent abstract is never read as a contradiction.

This is **abstract-only** in v1 (never full text) and **advisory**: it never changes a reference's
identification verdict. The JSON report carries the full per-citation `alignment_findings`
(status, evidence quote, confidence) for tooling and AI agents.

### Auditing a PDF

```bash
podman run -d --replace --name grobid -p 8070:8070 docker.io/grobid/grobid:0.9.1-crf   # wait ~30 s
uv run reference-audit audit paper.pdf
```

GROBID supplies both the reference list and the in-text citations, so the audit proceeds exactly as it
would from authored sources. What differs is that the input is now an *extraction*, and the report says
so rather than leaving you to assume otherwise:

- The header is marked `references extracted from PDF`, and an `INPUT NOTES` block lists what the
  extraction could not recover.
- Each entry shows the printed reference number (`[article] b12  (ref #13, cited)`), since the key is a
  GROBID-assigned id and means nothing on its own. A reference GROBID could not title is shown with the
  raw text it read, so you can find it in the PDF.
- Counts a PDF cannot support are not faked. Commented twins and unresolved `\input`s are omitted
  entirely. If GROBID links no in-text citation markers at all, the report says `citedness unknown` and
  lists *nothing* as uncited — rather than claiming every reference is uncited.
- A reference with neither a title nor an identifier is reported as uncheckable and no source is
  queried for it, instead of a title-less search returning an arbitrary paper that could be scored as a
  match.

Failures are named, never degraded into an empty bibliography: an unreachable or failing GROBID, a PDF
with no text layer, and a PDF with no detectable reference list each exit non-zero with the specific
reason (and, for an unreachable service, the command to start one).

### PDFs that embed their references

A PDF made with the [AI4X conference template](https://github.com/kazeevn/AI4X-2027-Template)
carries the BibTeX records of its cited works: the template's bibliography style (`ai4x.bst`, a copy
of `unsrturl.bst` that prints identically) writes each cited entry's record into the `.bbl`, and its
class embeds them as the PDF attachment `cited-references.bib.hex`. Only the cited entries are
embedded, with only the fields printed in the reference list plus identifiers (DOI, ISBN, arXiv id,
PubMed id), `@string` macros and `crossref` already resolved by BibTeX. The records are hex-encoded,
because they pass through TeX on the way to the attachment, where a raw `%`, `#` or `\` would break.

`reference-audit audit paper.pdf` looks for the attachment first. When it is there, the records are
audited exactly like a `.bib` — no GROBID, the same verdict cache, and `--no-network` works:

- The header reads `references embedded in PDF` (under `--no-network`, the usual `parse summary (no
  network)`), and every entry is cited (BibTeX exported only what it printed).
- A record is what the author compiled, not necessarily what is printed — a hand-edited `.bbl` can make
  them differ. Each record's title is therefore looked up in the PDF's printed reference list (after
  its last *References* or *Bibliography* heading; in the whole text if there is neither), and one
  that is not found is an issue on that entry, summarized in
  `INPUT NOTES` and in `summary.titles_not_in_printed_references`.
- The records carry no citing sentences, so `--check-citations` is declared unchecked in `INPUT NOTES`.
  `--ignore-embedded` takes the references, and the citing sentences, from GROBID instead.

A PDF without the attachment goes to GROBID as before. A malformed attachment, or a PDF the attachment
cannot be read from, goes to GROBID too, with the reason in `INPUT NOTES`.

## Development

```bash
uv run pytest          # unit + integration tests (databases & LLM are mocked)
REFERENCE_AUDIT_LIVE=1 uv run pytest -m clickhouse   # the live check of the local ClickHouse mirror
cfs validate           # validate the governance artifacts and code traceability
```

The unit and integration tests mock all database and LLM calls, so they are fast and offline. The
live tests (markers `clickhouse`, `grobid`, `pdf_build`) are not deselected by default: they run when
their service or toolchain is reachable and skip otherwise, unless `REFERENCE_AUDIT_LIVE=1` makes them
required. Add `-m "not clickhouse and not grobid and not pdf_build"` for a strictly offline run. When the tool gets a case
wrong, the fix is captured as a new test with the recorded response (see
[`architecture/SPEC.md`](architecture/SPEC.md), "General Design Principles").

### The PDF extraction oracle

Extraction fidelity is measured rather than assumed. Each test document is compiled to PDF from its own
`.tex` + `.bib`, so that `.bib` is ground truth for whatever GROBID reads back out; extracted references
are matched to printed ones by identifier and normalized title (there are no shared keys), and the
results are asserted against pinned floors.

These tests need a LaTeX toolchain and a running GROBID. When either is missing they **skip** with a
message naming what to start; with `REFERENCE_AUDIT_LIVE=1` they are **required**, and a missing
dependency fails instead of skipping — so a broken setup cannot sit green indefinitely.

```bash
uv run python tests/pdf_fixtures.py --all --preflight-only    # what would a compile need?
uv run python tests/pdf_fixtures.py --all --compile           # build the PDFs
REFERENCE_AUDIT_LIVE=1 uv run pytest -k pdf                   # measure, then assert the floors
```

Build artifacts land in a gitignored `tests/documents/<slug>/.build/<version>/`; nothing is written
next to the fixtures. The committed oracle is GROBID's TEI XML for the smallest document
(`tests/fixtures/tei/`, with a `.provenance.txt` recording the GROBID image, request parameters and
TeX Live version) — never a PDF. Regenerate it with the command recorded in that `.provenance.txt`
(`--slug … --version … --compile --grobid <url> --write-tei`). The one committed PDF,
`tests/fixtures/embedded/paper.pdf`, is not a GROBID oracle: it is compiled from the `.tex` + `.bib`
beside it with the AI4X template's `ai4x.cls` and `ai4x.bst`, because what it tests is the attachment
only a template-built PDF has.

Two deliberate behaviors of the harness:

- **Missing source files are never stubbed.** A compile that lacks an `\input` target, `.sty` or `.bst`
  fails with *every* missing file listed at once, so they can be supplied in one pass. Each document's
  `SOURCES.md` records where its auxiliary files came from.
- **Missing figures are the one substitution**, since a bibliography does not depend on them. They are
  replaced by generated placeholder images, and that substitution is stated in the build report and in
  the recorded fixture's provenance — page layout differs from the published article, which does not
  affect reference extraction.

The pinned numbers are a **regression floor for the recorded GROBID image and bibliography styles, not
a fidelity claim for arbitrary PDFs**: these bibliographies were typeset by BibTeX from clean data,
which is materially easier than what real-world PDFs contain.

### Benchmarking on HALLMARK

[HALLMARK](https://github.com/rpatrik96/hallmark) is a citation-hallucination benchmark. Each split is a
set of standalone BibTeX entries labelled `VALID` or `HALLUCINATED`, spanning 14 hallucination types in
3 difficulty tiers. `benchmarks/hallmark_bench.py` runs the full pipeline (network + LLM) on a split and
scores it with HALLMARK's own evaluator.

HALLMARK gets its own checkout and venv (`--hallmark-dir`, default `benchmarks/.hallmark`). `score` and
`submit` call its CLI (`--hallmark-bin`, default `<hallmark-dir>/.venv/bin/hallmark`); the harness never
imports it. Every subcommand takes `--split` (default `dev_public`) and `--out`, the run directory
(default `benchmarks/runs/hallmark/<split>`). Pin the commit so results stay reproducible:

```bash
git clone https://github.com/rpatrik96/hallmark benchmarks/.hallmark
git -C benchmarks/.hallmark checkout f774fa40675daa83eca6201637a94c4536b7bb3e
uv venv benchmarks/.hallmark/.venv --python 3.12
uv pip install --python benchmarks/.hallmark/.venv/bin/python -e benchmarks/.hallmark

uv run python benchmarks/hallmark_bench.py audit --split dev_public   # ~45 min on --backend clickhouse,
uv run python benchmarks/hallmark_bench.py score --split dev_public   #   ~1 h on the APIs
uv run python benchmarks/hallmark_bench.py submit --split dev_public  # the submission file
```

**`audit`** reads only the *blind* split (`<split>_blind.jsonl`), so labels never reach the tool.
- It writes the entries to `.bib` chunks of `--chunk-size` entries (default 200) and audits each with
  `run_audit`.
- Every chunk shares one cache (`--cache`, default `benchmarks/runs/hallmark/.cache/cache.db`).
- It keeps one compact record per entry in `benchmarks/runs/hallmark/<split>/audits.jsonl`.
- It refuses to run when no LLM key is configured, rather than silently measuring the formal-only
  pipeline. Pass `--no-llm` to measure that on purpose.
- The run is resumable chunk by chunk. Entries left unresolved, and entries whose audit failed, are
  re-audited `--retry-unresolved` times (default 1), since errors are never cached. `--limit N --seed S`
  audits a random sample.
- `--partial-authors ignore|warn|error` overrides `PARTIAL_AUTHORS`. HALLMARK counts an unmarked
  partial author list as a hallucination, so `error` scores the tool the way HALLMARK labels it.
- `--backend api|clickhouse` overrides `SOURCE_BACKEND` (see
  [Local ClickHouse backend](#local-clickhouse-backend)).
- A run directory refuses to resume under different settings: split, `--limit`, `--seed`,
  `pipeline_version`, model, whether the LLM is on, backend or partial-authors setting.
- `run.json` records:
  - those settings and both commits;
  - the outcome counts before and after the retry;
  - the wall time.

Before anything is audited, every record goes through a `.bib` write → `parse_bib` round-trip. A record
that does not survive it is reported, never audited as something else. In HALLMARK v1.2 these are
values truncated inside a brace, such as `Man{\'e`, which `parse_bib` reports as unparseable. They
become a not-evaluated prediction with the parser's reason, never a guessed label.

**`score`** writes `predictions.identity.jsonl` and `predictions.strict.jsonl`. It runs
`hallmark evaluate --eval-mode both` on each, restricted to the audited entries, and writes
`summary.md` with:
- the metrics, with HALLMARK's coverage next to them;
- per-type label counts;
- every false positive;
- the unresolved entries, with why;
- the misses by type.

A false positive or miss that HALLMARK itself relabelled (its `relabeled_from` / `relabel_reason`
fields) carries that history, since a disagreement on a relabelled entry is a candidate label error.
Eight `dev_public` entries labelled VALID are hallucinated: four cite another paper's DOI, three
cite authors who are not on the paper (two of them also a venue the paper never appeared in), and
one cites only a venue the paper never appeared in. HALLMARK
had labelled all eight HALLUCINATED and relabelled them on 2026-05-30. The evidence for each, with DOIs and DBLP keys to check it against, is in
[`benchmarks/hallmark_label_errors.md`](benchmarks/hallmark_label_errors.md).

Every audited entry gets a prediction: `score` stops unless the audited and labelled keys are the
same set. HALLMARK's own `--strict` is not used for this, because it counts an `evaluated=false`
prediction as missing, and a not-audited entry must stay a reported non-measurement. HALLMARK reports
such entries through `response_coverage`.

**`submit`** writes a run as a HALLMARK submission, in HALLMARK's `Prediction` JSONL schema. It writes
`benchmarks/submissions/hallmark/reference-audit-<mapping>_<split>_predictions.jsonl` and a manifest
`reference-audit-<mapping>_<split>.json` (`--mapping strict` by default; `--dest` moves them). The
manifest records the run's settings and commits, the label counts, and the SHA-256 of both the
predictions and the blind split.
- It reads no labels, so it also works for a split whose labels are withheld.
- It refuses a `--limit` sample, and a run whose records are not the blind split's keys one-to-one
  in split order.
- A not-audited entry stays `UNCERTAIN` with `evaluated=false`.
- HALLMARK's canary record (a `__canary__…` key, a watermark that HALLMARK never scores) is audited
  but left out of the file, and the manifest names it under `excluded_canaries`. `summary.md` leaves it
  out the same way.
- HALLMARK's own `hallmark validate-predictions` must accept the file before it replaces an earlier
  submission.

The submissions in the repository use `strict` with `--partial-authors error`, the setting that
scores the tool the way HALLMARK labels. They cover every split HALLMARK v1.2 ships a blind file for:
`dev_public`, `test_public` and `stress_test`. `test_hidden` is withheld by HALLMARK.

The two mappings exist because the two tools ask different questions. reference-audit's verdict asks
whether *any real document* corresponds to the entry. HALLMARK's `HALLUCINATED` also covers real papers
cited with wrong metadata (wrong venue, swapped or partial authors, a preprint cited as published, …),
which reference-audit reports as field findings rather than through the verdict.

| audit outcome | `identity` | `strict` |
| --- | --- | --- |
| `none` (high / medium / low confidence) | HALLUCINATED 0.9 / 0.75 / 0.6 | same |
| `exactly_one`, clean (high / medium / low) | VALID 0.9 / 0.75 / 0.6 | same |
| `exactly_one` + an `uncertain` field finding | VALID 0.6 | VALID 0.6 |
| `exactly_one` + an `unverifiable` field (e.g. a venue only an arXiv record was found for) | VALID 0.6 | UNCERTAIN 0.5 |
| `exactly_one` + an `error` field finding (a wrong title, venue, year or DOI, or a cited author who is not an author of the work) | VALID 0.6 | HALLUCINATED 0.7 |
| `multiple`, or unresolved | UNCERTAIN 0.5 | same |
| not audited (round-trip failure, or the audit raised) | UNCERTAIN 0.5, `evaluated=false` | same |

The numbers are HALLMARK's confidence, i.e. P(label is correct). They are a fixed table, not fitted to
the labels.

HALLMARK's two scoring modes treat UNCERTAIN differently:
- **Conservative** drops UNCERTAIN from the classification metrics.
- **Aggressive** counts UNCERTAIN as HALLUCINATED.

Both modes exclude `evaluated=false` predictions.

A cited DOI that belongs to a different paper, or that does not resolve, is an `error` field finding
on the `doi` field (see [How it works](#how-it-works)). `strict` therefore counts it as HALLUCINATED;
`identity` does not, since a real document still matches.

#### Result: `dev_public`, pipeline 0.23

The results below, and the committed submissions, were measured at pipeline 0.23. The current
pipeline is 0.25, and it has not been re-measured:
- 0.24: bibtexparser 2. `.bib` values are read by the tool itself and LaTeX is decoded with
  pylatexenc, which changes some field values and which entries are unparseable.
- 0.25: HTML character references in source values are decoded, so a source title with `&amp;` is
  settled by rule instead of going to the field-check LLM.

The harness refuses to resume a run directory from another pipeline version; re-measure into a new
`--out`.

This run was on 2026-10-09 with the ClickHouse backend and `gpt-6-luna`, at HALLMARK `f774fa4` and
reference-audit `7e9933c`. All 1,119 entries were run:
- 1,112 were audited; 7 failed the `.bib` round-trip and are not evaluated.
- 8 HALLUCINATED entries stay unresolved at the per-entry LLM candidate cap.

The two `--partial-authors` settings give the same verdicts and differ only in the `author` finding
for an unmarked partial list. `error` scores the tool the way HALLMARK labels such a list.

| mapping | partial lists | mode | DR | FPR | F1 | MCC | Tier-3 F1 | coverage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `identity` | either | conservative | 0.415 | 0.002 | 0.585 | 0.494 | 0.659 | 0.984 |
| `identity` | either | aggressive | 0.425 | 0.002 | 0.596 | 0.500 | 0.662 | 0.984 |
| `strict` | `warn` | conservative | 0.948 | 0.018 | 0.966 | 0.928 | 0.969 | 0.982 |
| `strict` | `warn` | aggressive | 0.949 | 0.022 | 0.965 | 0.925 | 0.964 | 0.982 |
| `strict` | `error` | conservative | 0.995 | 0.018 | 0.990 | 0.978 | 0.969 | 0.982 |
| `strict` | `error` | aggressive | 0.995 | 0.022 | 0.988 | 0.975 | 0.964 | 0.982 |

- **`identity`** flags 1 of 513 VALID entries: Flamingo, one of the eight label errors above, which
  gets *no match* because 4 of its 19 cited authors are not on the paper. It finds only
  hallucinations with no real counterpart, since its verdict is about existence, not metadata.
- **`strict`** flags 9 VALID entries. Eight are hallucinated despite their label (see above). The
  ninth is a 2026 preprint whose arXiv title changed after it was cited, which no source still
  records.
- **`strict` with `error`** misses 3 hallucinations:
  - an author-order swap (the author check is order-free);
  - two near-miss titles that differ only in a hyphen ("Schema Variable" / "Schema-Variable"),
    which the tool treats as formatting.

At pipeline 0.21, `strict` had DR 0.951, FPR 0.094, F1 0.945, MCC 0.860 and coverage 0.777
(conservative), and FPR 0.392 aggressive. The changes since are listed under `pipeline_version` in
`config.py`:
- 0.22: version-aware field checks; person-level authors; no fusing of a paper with its journal
  extension; the field-check LLM's context; the doi.org Handle API.
- 0.23: a field's value is the one most sources of the cited version agree on; a pooled record's
  author list comes from the most reliable source; pooled members are deduplicated; given-name forms
  and dropped middle initials match; a different journal or conference series is a field error.

A cold run takes about 45 minutes; with warm source and LLM caches, minutes.

#### Result: `test_public` and `stress_test`, pipeline 0.23

These were run on 2026-10-09 with the same settings: ClickHouse, `gpt-6-luna`, `--partial-authors error`,
HALLMARK `f774fa4`, and reference-audit `f100559` (docs-only after `7e9933c`). They are what the
submissions in `benchmarks/submissions/hallmark/` contain.
- **`test_public`**: all 831 entries ran in 21 minutes on a mostly cold cache.
  - 2 entries failed the `.bib` round-trip and are not evaluated.
  - 2 HALLUCINATED entries stay unresolved at the LLM candidate cap.
- **`stress_test`**: all 122 entries ran in 7 minutes. One of them is HALLMARK's canary, which is not
  scored, so 121 entries are scored, all of them HALLUCINATED.

| split | mapping | mode | DR | FPR | F1 | MCC | Tier-3 F1 | coverage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `test_public` | `identity` | conservative | 0.424 | 0.003 | 0.595 | 0.461 | 0.652 | 0.990 |
| `test_public` | `identity` | aggressive | 0.429 | 0.010 | 0.598 | 0.456 | 0.649 | 0.990 |
| `test_public` | `strict` | conservative | 0.988 | 0.020 | 0.988 | 0.969 | 0.964 | 0.986 |
| `test_public` | `strict` | aggressive | 0.988 | 0.032 | 0.985 | 0.959 | 0.952 | 0.986 |
| `stress_test` | `identity` | either | 0.364 | — | 0.533 | — | 0.400 | 1.000 |
| `stress_test` | `strict` | either | 0.967 | — | 0.983 | — | 0.971 | 1.000 |

`stress_test` has no VALID entries, so FPR is undefined and MCC is degenerate (HALLMARK reports 0);
HALLMARK says to read it by DR.

- **`test_public`, `strict`**: 6 false positives among 312 VALID entries.
  - HALLMARK relabelled four of them HALLUCINATED → VALID. Three cite a DOI that belongs to another
    paper (Mip-NeRF, MAE) or a venue the paper did not appear at (MiniGPT-4, cited at CVPR). The
    fourth is LLaMA 2, where 20 cited names are not on the matched record. Some are real authors
    missing from the source record (Wenhan Xiong, Dmitriy Liskovich); others look fabricated
    ("Yuchen Zarov", from Yuchen Zhang and Iliyan Zarov). None of the four has yet been checked the
    way `benchmarks/hallmark_label_errors.md` checks the `dev_public` ones.
  - One is a name variant the tool does not match: Ebrahimianghazani / Ebrahimian.
  - One is a real paper the tool found no match for: the NSGA-II runtime analysis. This is also
    `identity`'s only false positive.
- **`test_public`, `strict` misses (6)**:
  - four near-miss titles that differ only in a hyphen or in British/American spelling
    ("Minimisation");
  - one wrong venue (AISTATS for ICML) and one arXiv year, which the field check left `uncertain`
    instead of `error`.
- **`stress_test`, `strict` misses (4)**:
  - two partial author lists the author check did not flag;
  - two arXiv version mismatches whose year is one off, which the field check leaves `uncertain`.

## Constructor Fabric

This project is built and governed with [Constructor Fabric's `studio`](https://github.com/constructorfabric/studio)
(the `cfs` CLI, distributed as the `constructor-studio` package). Studio is a **governance and
traceability framework for AI-assisted delivery** — it is *not* a runtime library, and the audit
pipeline does not call it. The auditor itself is plain async Python; studio governs *how* that code
is specified, traced, and validated.

Concretely:

- **Governed artifacts** live in [`architecture/`](architecture/) — the specification and design
  (SPEC, PRD, DESIGN, DECOMPOSITION) plus per-feature documents
  ([`features/parsing.md`](architecture/features/parsing.md),
  [`features/identification.md`](architecture/features/identification.md),
  [`features/citation-alignment.md`](architecture/features/citation-alignment.md),
  [`features/pdf-input.md`](architecture/features/pdf-input.md)). These are the source of
  truth for what the system is meant to do.
- **Code traceability** links implementation back to those artifacts via `@cpt-*` markers in
  `src/`, so each governed requirement maps to the code that fulfills it. Both the offline parse
  slice and the networked identification/verdict pipeline are traced to code.
- **The validation gate** ties it together:

  ```bash
  cfs validate    # validate the governance artifacts and code traceability
  ```

- **Studio's configuration** lives in `.cf-studio/` (the installed SDLC kit defines the required
  artifact structure). The root `AGENTS.md` is tracked: its leading `@cf:root-agents` block is
  studio's, and the project rules after it are written by hand. The root `CLAUDE.md` is generated by
  `cfs update` on every run and is gitignored.

`constructor-studio` is not a project dependency: install the `cfs` CLI once, globally, as a uv tool:

```bash
uv tool install git+https://github.com/constructorfabric/studio.git
```

### Using Constructor Fabric

You drive studio two ways: directly through the `cfs` CLI, and through your AI assistant, which
follows studio's governed SDLC workflows.

**1. The `cfs` CLI (run these yourself).** With the uv tool installed, `cfs` is on your `PATH`:

```bash
cfs validate          # the gate: artifact structure + code traceability must pass
cfs doctor            # environment health check
cfs info              # show project configuration and resolved kit paths
cfs spec-coverage     # report @cpt marker coverage across the codebase
cfs list-ids          # list every cpt-* ID and where it is defined
cfs where-defined cpt-referenceaudit-fr-identify-artifact   # jump to a definition
cfs where-used    cpt-referenceaudit-fr-identify-artifact   # find every reference
cfs map --out map.html   # interactive markdown↔source traceability graph
cfs check-updates     # check studio + installed kits for updates
```

`cfs --help` lists most of the command set (`check-updates` is not in it, but works and has its own
`--help`). **`cfs validate` is the one to run before
every commit** — it is the contract that the `architecture/` artifacts and the `@cpt-*` markers in
`src/` agree.

**2. AI-assisted SDLC workflows.** Studio ships the `sdlc` kit (under `.cf-studio/`) with governed
workflows — `doc-prd`, `doc-design`, `decompose`, `doc-feature`, `implement`, `pr-review`,
`reverse-engineer`, and more. Generate the integration for your editor/agent once:

```bash
cfs generate-agents   # writes agent/command/rule files for Claude, Cursor, Copilot, …
```

Then ask your assistant for the task in natural language and it follows the matching workflow — for
example *"spec a new FEATURE for the canonical .bib output"* runs the `doc-feature` authoring loop
(template → deterministic gate → semantic review), and *"implement that feature"* runs `implement`,
adding `@cpt` markers and syncing the DECOMPOSITION checkboxes. The root `AGENTS.md` (and the
generated `CLAUDE.md`) bind these rules into the assistant's context.

**The everyday loop** when you change behavior — manually or via the assistant — is always: update
the governing artifact (PRD/DESIGN/DECOMPOSITION/feature) → update the code and its `@cpt` markers →
run `cfs validate` until green → update `README.md`. A `flow`/`algo`/`dod`/`state` definition
checked `[x]` in a FEATURE **requires** a matching code marker; unchecked **forbids** one. If a
capability is implemented but not yet `@cpt`-traced, leave its box unchecked and say so, rather than
claiming coverage you don't have.

**Updating studio itself:**

```bash
uv tool upgrade constructor-studio   # upgrade the cfs CLI itself
cfs update                           # refresh the project's studio install (kits are left alone unless you pass --with-kits yes)
```

## Project layout

```
src/reference_audit/
  parsing/     # .bib / .tex / identifier parsing; latex.py = LaTeX → Unicode for .bib values;
               #   entities.py = HTML character references; context.py = the shared sentence definition
  pdf/         # PDF input: grobid.py (the only network module — one client, one named error per
               #   failure mode) + tei.py (pure TEI -> BibEntry / CitationContext mapping) +
               #   embedded.py (the BibTeX records a PDF embeds, and the printed-title check)
  sources/     # modular adapters: Crossref, OpenAlex, Semantic Scholar, arXiv, DBLP, Open Library,
               #   Google Books, publisher (DOI landing-page citation export), web (cited-page fetch),
               #   render (headless-browser rendering of JS single-page-app pages); registry.py =
               #   routing; base.py = the adapter contract; normalize.py = raw JSON → SourceRecord;
               #   http.py = per-source spacing, in-flight caps and retries;
               #   clickhouse.py = the local-mirror backend for S2/OpenAlex/DBLP; titlewords.py = the
               #   searchable words of a title (shared by every all-words title search)
  matching/    # candidate pooling, feature scoring, author-name matching (names.py), LLM
               #   adjudication of undecided candidates, SAME-OBJECT clustering, verdicts, web check
  llm/         # OpenAI structured output (pydantic schemas): adjudication, field and alignment prompts
  cache/       # SQLite memoization of DB/LLM calls (errors never cached)
  alignmentcheck.py # --check-citations: citing context vs. the cited work's abstract
  bookcheck.py # Open Library edition resolution (cited vs. latest edition)
  fieldcheck.py# per-field correctness / formatting findings for an exactly-one match
  versioning.py# better-version detection (published > preprint)
  pipeline.py  # async orchestration
  report.py    # text / JSON rendering
  config.py    # AuditConfig (model, keys, thresholds)
  models.py    # pydantic domain models
  inputs.py    # which input shape the CLI arguments name (.tex + .bib, or a .pdf)
  cli.py       # command-line entry point (Typer)
architecture/  # governed specification & design (SPEC, PRD, DESIGN, DECOMPOSITION, features)
benchmarks/
  hallmark_bench.py # HALLMARK harness: audit a blind split, map verdicts to labels, run HALLMARK's
                    #   evaluator, write submissions (needs the pinned checkout in benchmarks/.hallmark)
  submissions/hallmark/ # HALLMARK submission files (predictions JSONL + manifest), one per split
  hallmark_label_errors.md # dev_public entries labelled VALID that are hallucinated, with evidence
tests/
  documents/   # test papers: <paper-title-slug>/<version>.{tex,bib} (initial, polished, …),
               #   plus the auxiliary LaTeX sources needed to compile them (see each SOURCES.md)
  fixtures/tei/# recorded GROBID output + provenance, so the mapper is verifiable offline
  fixtures/embedded/ # an AI4X-template PDF that embeds its references' BibTeX records, + its sources
  fixtures/dblp/     # a recorded DBLP SPARQL response + provenance
  pdf_fixtures.py # not a test: preflight + compile + TEI capture, runnable by hand
  pdf_oracle.py   # matches extracted references to printed ones and scores the difference
  *.py         # mocked unit/integration tests, plus the live ClickHouse / PDF tests (skipped when
               #   their service is missing); the pilot paper is the development oracle
```

See [`architecture/SPEC.md`](architecture/SPEC.md) for the full specification and the
`architecture/` artifacts for the detailed design.
