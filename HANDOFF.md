# Handoff: reference-audit on the HALLMARK benchmark

Updated 2026-10-09, for the next session to continue from a fresh context.
Branch `feat/hallmark-clickhouse`, pushed to `origin` after every step. The user asked for commits
and pushes straight away, with no PR. `main` is unchanged.

## Goal

Measure reference-audit on [HALLMARK](https://github.com/rpatrik96/hallmark) `dev_public` (1,119
entries: 513 VALID, 606 HALLUCINATED), and fix what the measurement exposes. The user wanted:
- leaderboard-comparable metrics;
- per-type detection rates;
- a work list of false positives and misses;
- a switch for partial author lists;
- the HALLMARK label errors documented.

## Status: done at pipeline 0.23

Final runs, at commit `7e9933c` (gitignored, on this machine):
- `benchmarks/runs/hallmark/dev_public/` with `--partial-authors warn`, the default;
- `benchmarks/runs/hallmark/dev_public-partial-error/` with `--partial-authors error`.

The result table is in README, "Benchmarking on HALLMARK → Result". `strict` conservative with
`error`: DR 0.995, FPR 0.018, F1 0.990, MCC 0.978, coverage 0.982.

Earlier runs, kept for comparison:
- `dev_public-0.21`: DR 0.951, FPR 0.094, F1 0.945, coverage 0.777;
- `dev_public-0.22`;
- `dev_public-0.23-first`.

`benchmarks/hallmark_label_errors.md` documents eight entries labelled VALID that are hallucinated,
each with DOIs and DBLP keys to verify it:
- four cite another paper's DOI;
- three cite authors who are not on the paper;
- one cites a venue PaLM never appeared at.

Every one of them was relabelled HALLUCINATED → VALID by HALLMARK's `systematic-relabel-2026-05-30`.

### What changed this session (all pushed)

- **Harness:**
  - `score` crashed on relabelled rows without type keys;
  - HALLMARK's `--strict` rejects `evaluated=false`, so the harness checks completeness itself;
  - wall time now accumulates over resumed runs;
  - new `--partial-authors`.
- **Source limits:**
  - Crossref allows 3 requests in flight, arXiv 1, with arXiv spaced at 1 per 3 s;
  - every retry goes through the limiter.
- **Pipeline 0.22:**
  - pooled records keep their `members`;
  - field checks compare against the version the entry cites;
  - authors are checked person by person, as an `author` field finding with
    `--partial-authors ignore|warn|error` (default `warn`);
  - pooling no longer fuses a paper with a journal extension that has other authors;
  - the field-check LLM sees the matched work, not the entry;
  - cited DOIs are checked with the doi.org Handle API.
- **Pipeline 0.23:**
  - a field's canonical value is the one most sources of the cited version agree on;
  - the pooled author list comes from the most reliable source;
  - members are deduplicated;
  - given-name forms (Tim / Timothy) are recognised;
  - the prompt calls a different venue series an error.
- **After 0.23, findings only, no version bump:**
  - a partial list must be shorter than the record's;
  - Handle API code 301 means unregistered.

## Remaining work list (not fixed; raise with the user)

1. **Renamed preprints.** A 2026 preprint whose arXiv title changed after citation gets a title error
   (`a0527a7c2d1b`, the one non-label-error FP). arXiv's API gives only the latest title.
2. **Author order is not checked.** An adjacent swap passes (`be764c4d9889`).
3. **Hyphen-only near-miss titles are treated as formatting** (`cc3bac858db2`, `1cc022db3273`). This
   is a policy question: HALLMARK calls them hallucinations.
4. **8 entries are unresolved at `llm_max_candidates=8`.** Raise the cap? That's the user's call.
5. **3 `fabricated_doi` entries end `multiple`** (`abd68711ff28`, `cf9b91805136`, `d0a040eb49c2`).
   Not diagnosed.
6. **Name variants still not matched**, harmless now for the partial-list check but visible to the
   pooling guard:
   - joined/split tokens (`RichardWebster`);
   - `ß` / `ss` (`Reiß` / `Reis`);
   - Russian diminutives (`Misha` / `Mikhail`).

   Fixing them changes `same_person`, which pooling uses, so it needs a version bump.
7. **LLM adjudication is nondeterministic (temperature 1)** on borderline cases. Flamingo, with 4
   fabricated authors, flipped between `exactly_one` and `none` between runs.
8. **Minor:**
   - the arXiv base URL is `http://` (a 301 on every query);
   - a resolved verdict is cached even when a source errored, with a thinner artifact.

## Machine state outside git (this machine only)

- `benchmarks/.hallmark/`: HALLMARK at `f774fa40675daa83eca6201637a94c4536b7bb3e`, with its own venv.
- `.env`:
  - `SOURCE_BACKEND=clickhouse`, the ClickHouse credentials and `OPENAI_API_KEY` (`gpt-6-luna`);
  - the polite-pool mailto comes from `PAPER_SEARCH_MCP_UNPAYWALL_EMAIL`, set by the user.
- `benchmarks/runs/hallmark/.cache/cache.db` is warm for 0.23; a rerun at 0.23 takes minutes.
- Delete these once no longer needed; nothing reads them:
  - `.cache/cache.db.pre-0.20`;
  - `.cache/cache.db.throttled-0.21{,-wal,-shm}`;
  - `dev_public-throttled/`.

## Decisions the user made (do not re-ask)

- `dev_public` first; the harness lives in the repo with docs and `cfs` updates.
- DBLP via SPARQL; the backend is a per-run switch (`api` default, `.env` selects `clickhouse`).
- I may add indexes to the local ClickHouse.
- Fix items 1–5 of the 0.21 work list (done); the partial-author treatment is a CLI/config switch
  (done); commit and push straight away, no PR; document the HALLMARK label errors (done).

## Rules and gotchas

- AGENTS.md applies:
  - bump `AuditConfig.pipeline_version` on any verdict-affecting change;
  - update README and `architecture/` in the same change, and keep `uv run cfs validate` green.

  Field findings are recomputed on every verdict-cache hit, so a findings-only change needs no bump.
- Before editing governed docs, the `cf-studio` prerequisites apply (a phase plan, a DoD and
  acceptance criteria). Writing them in the scratchpad satisfies them.
- **Never send the user's email address to an external service.**
- HALLMARK `evaluate`:
  - conservative excludes UNCERTAIN, and aggressive counts it as HALLUCINATED;
  - `evaluated=false` is excluded from both.

  Report coverage next to the metrics.
- `run.json` records the commit at the *end* of a run. Do not leave tracked files modified while a run
  is going; use a git worktree for parallel code work.
- Do not run probes in parallel with a benchmark run: each process has its own Crossref/arXiv cap.
- When moving a SQLite cache aside, move its `-wal` / `-shm` files with it.
- `clickhouse-connect`:
  - use the sync client with `autogenerate_session_id=False`;
  - search words must be lower-case.
