# Handoff: running reference-audit on the HALLMARK benchmark

Written 2026-10-09 at the end of a session, for the next session to continue from a fresh context.
Branch `feat/hallmark-clickhouse` (pushed to `origin`; no PR opened). `main` is unchanged.

## Goal

Measure reference-audit on [HALLMARK](https://github.com/rpatrik96/hallmark), a
citation-hallucination benchmark. Scope so far is the `dev_public` split, 1,119 entries: 513 VALID and
606 HALLUCINATED, across 14 types in 3 tiers. The user wants three things:
- metrics comparable to the HALLMARK leaderboard;
- per-type detection rates;
- the concrete false positives and misses, as a work list for improving the tool.

## Status: the infrastructure is done, the measurement has not been run

Done and committed on this branch:

1. **HALLMARK harness**: `benchmarks/hallmark_bench.py`, tests in `tests/test_hallmark_bench.py`.
   - **`audit`** reads the *blind* split only. It runs a `.bib` round-trip preflight, then audits in
     resumable chunks through `run_audit`, with one shared cache. It retries unresolved/failed
     entries once and writes `audits.jsonl` plus `run.json`.
   - **`score`** maps every audit to a HALLMARK prediction under two fixed mappings, runs
     `hallmark evaluate --eval-mode both --strict`, and writes `summary.md`. The mappings:
     - `identity`: the verdict alone. `none` is HALLUCINATED, `exactly_one` is VALID.
     - `strict`: as identity, plus a confirmed metadata error on an `exactly_one` match (an `error`
       field finding, or a cited author missing from the matched record) counts as HALLUCINATED.
   - The mapping table and confidences are in the README, "Benchmarking on HALLMARK".
2. **DBLP via SPARQL.** DBLP's search API, and every mirror, now answers automated clients with an
   Anubis "not a bot" page. That made every DBLP query an error, so every unmatched entry came out
   *unresolved* instead of `none`. `sources/dblp.py` now queries `sparql.dblp.org`.
3. **Local ClickHouse backend** for Semantic Scholar, OpenAlex and DBLP, selected per run:
   `SOURCE_BACKEND=clickhouse` in `.env`, or `--backend clickhouse`. `api` remains the default.
   - The code is `sources/clickhouse.py`, with tests in `tests/test_clickhouse.py`; the one live test
     needs the server.
   - Title search uses new full-text indexes (`idx_title_text`), which I added to `s2ag.papers`,
     `openalex.works_slim` and `kb.dblp_publication`. All are fully built: 7.1 GiB, 13.6 GiB and
     181 MiB.
   - A preflight refuses an unreachable mirror, or a missing or still-building index.
   - The verdict cache is now keyed by `(entry_hash, backend)`, and old caches migrate in place.
4. `pipeline_version` is **0.19**. README, `architecture/` and AGENTS.md (the cache-gate sentence)
   are updated. `uv run cfs validate` and `uv run pytest` pass (456 passed, 7 skipped).
5. `.claude/settings.json` enables the ClickHouse agent-skills plugin (`clickhouse-best-practices`)
   for this project.

**Not done:**
- The full `dev_public` audit.
- The `score` step. It has **never been run end to end**, so the `hallmark evaluate` invocation and
  `summary.md` generation are untested against real output.
- The final report to the user.

## Machine state outside git (this machine only)

- `benchmarks/.hallmark/`: HALLMARK cloned at the pinned commit
  `f774fa40675daa83eca6201637a94c4536b7bb3e`, with its own Python 3.12 venv in
  `benchmarks/.hallmark/.venv`. It needs its own venv because HALLMARK pins `bibtexparser>=2` and we
  pin `<2`. The directory is gitignored.
- `.env` (gitignored) has `SOURCE_BACKEND=clickhouse` and `CLICKHOUSE_HOST/PORT/USER/
  DEFAULT_USER_PASSWORD`; the password was copied from `/home/kna/keynote-beagle/.env`. The
  `OPENAI_API_KEY` there works with the default model `gpt-6-luna`.
- `benchmarks/runs/hallmark/.cache/cache.db` (gitignored) is the shared harness cache. It holds the
  smoke runs' results.
- `.cf-studio/.core` and `.gen` were missing, so I restored them with `uv run cfs init --yes`. Be
  aware that this command also:
  - rewrites tracked files: `.cf-studio/version.toml`, `whatsnew.toml`, `config/README.md` and
    `.gitignore`;
  - recreates `CLAUDE.md`, which the user deliberately deleted in commit 9c828c4.

  I reverted both. Do the same if it is ever re-run.

## Smoke results so far (40 random `dev_public` entries, `--limit 40 --seed 0`)

- **API backend, pipeline 0.17, before the DBLP fix:** 33 `exactly_one`, 1 `multiple`, 6
  unresolved, 0 `none`, in 93 s. Every unresolved entry was a DBLP error, which led to fix 2.
- **ClickHouse backend, pipeline 0.19:** 32 `exactly_one`, 7 `none`, 1 `multiple`, 0 unresolved,
  in 31 s, with 25 LLM adjudications. The outputs are in the session scratchpad and may be gone.

## Next steps

All commands run from the repo root.

1. **Exercise `score` on a small run first**, since it has never been run:
   ```bash
   uv run python benchmarks/hallmark_bench.py audit --split dev_public --limit 40 --seed 0 \
       --out benchmarks/runs/hallmark/smoke40
   uv run python benchmarks/hallmark_bench.py score --split dev_public --out benchmarks/runs/hallmark/smoke40
   ```
   Then check `eval.*.txt` / `eval.*.json` and `summary.md`. `score` restricts HALLMARK's data dir to
   the audited keys, so `--strict` works on a sample. `build_summary` expects `eval.<mapping>.json`
   to hold `{"conservative": …, "aggressive": …}`, which is what `--eval-mode both --output` writes
   per `hallmark/cli.py`. Fix whatever breaks, with a test.
2. **Run the full split.** It should take minutes on ClickHouse; use `run_in_background` and monitor:
   ```bash
   uv run python benchmarks/hallmark_bench.py audit --split dev_public > benchmarks/runs/hallmark/dev_public.log 2>&1
   uv run python benchmarks/hallmark_bench.py score --split dev_public
   ```
   The default output directory is `benchmarks/runs/hallmark/dev_public`.
3. **Report to the user:**
   - the metrics table (identity/strict × conservative/aggressive);
   - per-type detection rates;
   - the false-positive list;
   - the misses by type;
   - the tool gaps the run exposes.

   Then mark the acceptance criterion in `architecture/features/identification.md` §6 ("Verdict
   accuracy is measured on the HALLMARK `dev_public` split…") as `[x]`.
4. Optional, if the user wants it: `test_public` (831 entries), or an API-backend run for
   comparison. The API run is about 1 h, because Semantic Scholar is limited to 1 request/s.

## Known tool gaps (found this session, not fixed, worth raising with the user)

- **DOI conflicts are never reported.** A cited DOI that does not resolve, or that belongs to another
  paper, raises no finding when the title and authors match a real work. `fabricated_doi` and
  `hybrid_fabrication` are caught only when the rest of the entry fails too. A workaround would be to
  compare `entry.ids.doi` with `verdict.artifacts[0].merged_ids.doi`. Fixing it is a
  verdict/findings change, so it needs a `pipeline_version` bump.
- **`parse_bib` silently drops an entry with unbalanced braces.** bibtexparser 1.x skips it, and
  nothing is reported unless the key is explicitly `\cite`d. That violates the "never fail silently"
  rule in AGENTS.md. Nine HALLMARK records hit this: 7 in dev, 2 in test, e.g. `Man{\'e`. The harness
  reports them as not evaluated.
- **An unresolved entry does not say why.** `_gather_candidates` in `pipeline.py` collapses source
  errors into a boolean, so the report cannot name the failing source.
- **The local ClickHouse backend is less forgiving than the APIs.** All title words must match, so a
  cited title with an extra or misspelt word finds nothing locally. OpenAlex has no `locations`
  locally, so version links are lost. The DBLP dump has no landing page or pages. Each snapshot
  ends at its own date: OpenAlex 2026-06-26, DBLP 2026-09-19. Watch for VALID entries flagged `none`
  that the API would have found.
- **A local S2 title search costs about 1.8 s cold.** The text index is per part, and its filter is
  evaluated across a 198 M-row part. This is acceptable now; finer index granularity could help.

## Decisions the user made (do not re-ask)

- Run on `dev_public` first.
- The harness is committed in the repo, under `benchmarks/`, with docs and `cfs` updates.
- DBLP is ported to SPARQL, not waited out.
- The backend is a per-run switch. `api` is the default; this repo's `.env` selects `clickhouse`.
- I may add indexes to the local ClickHouse.

## Rules and gotchas

- AGENTS.md applies:
  - bump `AuditConfig.pipeline_version` on any verdict-affecting change;
  - update README and `architecture/` in the same change, and keep `uv run cfs validate` green;
  - a failure is reported, never defaulted.
- Before editing governed docs, the `cf-studio` `doc-feature` → `documenting-gen` workflow
  prerequisites apply: a phase plan, a DoD and acceptance criteria.
- **Never send the user's email address to an external service.** I once put it in a test
  User-Agent sent to dblp.org; don't repeat that.
- HALLMARK's `evaluate` treats UNCERTAIN differently from what its README says:
  - **conservative** *excludes* UNCERTAIN from the classification metrics (the README claims
    UNCERTAIN counts as VALID);
  - **aggressive** maps UNCERTAIN to HALLUCINATED;
  - `evaluated=false` predictions are excluded in both modes.

  Always report coverage next to the metrics.
- `clickhouse-connect`:
  - use the sync client with `autogenerate_session_id=False`, since concurrent queries in one
    session fail;
  - search words must be lower-case (`hasAllTokens` with the `lower()` preprocessor index);
  - read-only probe scripts against ClickHouse can use keynote-beagle's venv
    (`/home/kna/keynote-beagle/.venv/bin/python`) or this project's.
