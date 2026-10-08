# Handoff: running reference-audit on the HALLMARK benchmark

Updated 2026-10-09, for the next session to continue from a fresh context.
Branch `feat/hallmark-clickhouse`. Commits `c5226b5` (harness, DBLP via SPARQL, ClickHouse backend) and
`c331de5` are pushed to `origin`. `b734b1a` (the tool-gap fixes) and this HANDOFF update are **local
only**: push them when the user agrees. No PR is open, and `main` is unchanged.

## Goal

Measure reference-audit on [HALLMARK](https://github.com/rpatrik96/hallmark), a
citation-hallucination benchmark. Scope so far is the `dev_public` split, 1,119 entries: 513 VALID and
606 HALLUCINATED, across 14 types in 3 tiers. The user wants three things:
- metrics comparable to the HALLMARK leaderboard;
- per-type detection rates;
- the concrete false positives and misses, as a work list for improving the tool.

## Status: the tool is ready, the measurement has not been run

Done and committed on this branch:

1. **HALLMARK harness**: `benchmarks/hallmark_bench.py`, tests in `tests/test_hallmark_bench.py`.
   - **`audit`** reads the *blind* split only. It runs a `.bib` round-trip preflight, then audits in
     resumable chunks through `run_audit`, with one shared cache. It retries unresolved/failed
     entries once and writes `audits.jsonl` plus `run.json`.
   - **`score`** maps every audit to a HALLMARK prediction under two fixed mappings, runs
     `hallmark evaluate --eval-mode both --strict`, and writes `summary.md`. The mappings:
     - `identity`: the verdict alone. `none` is HALLUCINATED, `exactly_one` is VALID.
     - `strict`: as identity, plus a confirmed metadata error on an `exactly_one` match counts as
       HALLUCINATED. That is an `error` field finding (now including a wrong `doi`), or a cited
       author missing from the matched record.
   - The mapping table and confidences are in the README, "Benchmarking on HALLMARK".
   - `summary.md` also lists the unresolved entries with their reasons.
2. **DBLP via SPARQL** (`sources/dblp.py`). DBLP's search API answers automated clients with a
   bot-challenge page.
3. **Local ClickHouse backend** for Semantic Scholar, OpenAlex and DBLP (`sources/clickhouse.py`),
   selected by `SOURCE_BACKEND=clickhouse` in `.env` or `--backend clickhouse`; `api` remains the
   default. The full-text indexes `idx_title_text` are built on `s2ag.papers`,
   `openalex.works_slim` and `kb.dblp_publication`.
4. **The tool gaps the last session found are fixed** (`b734b1a`, `pipeline_version` **0.20**):
   - **Cited DOI check.** A cited DOI the matched work does not carry becomes a `doi` field finding:
     - `error` when it belongs to another work (the finding names it) or doi.org answers 404;
     - `uncertain` when it cannot be tied to the match;
     - `unverifiable` when nothing answers.

     Pooling now keeps every member record's DOI in `raw["merged_dois"]`. Before that, a published
     DOI folded into an arXiv-led pooled record looked foreign. `strict` scores a `doi` error as
     HALLUCINATED.
   - **Unparseable `.bib` entries.** `parse_bib` returns a third value, `unparsed`. The report has
     an `UNPARSEABLE .bib ENTRIES` section, and the harness uses the parser's reason.
   - **Unresolved entries say why**, in `EntryAudit.unresolved_reasons` and as `unresolved: …`
     issues: the failed source and query kind, an LLM error, LLM off, a low-confidence ruling, or
     the LLM cap.
   - **ClickHouse search.** When no all-words hit is a near-exact title, the search is retried with
     each word left out (≥ 4 distinct words), ranked by edit distance; one wrong word no longer hides
     the paper. OpenAlex rows gain `best_oa_location` as a version link. Preflight reads each
     snapshot's end (`coverage_end`), the report header shows it, and a `none` verdict for an entry
     from that year or later gets a coverage-gap note.
   - **Found and fixed on the way: a cache-slot collision.** Enrichment by a matched or backfilled
     identifier was cached in the entry's own by-id slot, so later runs read the matched work's
     records as the entry's own lookup. It is now cached under a probe entry.
5. README, PRD, DESIGN and the `parsing` / `identification` feature docs are updated.
   `uv run cfs validate` passes, and `uv run pytest` gives 477 passed, 7 skipped.
6. `.claude/settings.json` enables the ClickHouse agent-skills plugin for this project.

**Not done:**
- The full `dev_public` audit.
- The `score` step. It has **never been run end to end**, so the `hallmark evaluate` invocation and
  `summary.md` generation are untested against real output.
- The final report to the user.

## Machine state outside git (this machine only)

- `benchmarks/.hallmark/`: HALLMARK cloned at the pinned commit
  `f774fa40675daa83eca6201637a94c4536b7bb3e`, with its own Python 3.12 venv in
  `benchmarks/.hallmark/.venv` (HALLMARK pins `bibtexparser>=2`; we pin `<2`). Gitignored. The
  labelled split is `benchmarks/.hallmark/data/v1.2/dev_public.jsonl`; the harness audits
  `dev_public_blind.jsonl`.
- `.env` (gitignored) has `SOURCE_BACKEND=clickhouse` and `CLICKHOUSE_HOST/PORT/USER/
  DEFAULT_USER_PASSWORD`; the password was copied from `/home/kna/keynote-beagle/.env`. The
  `OPENAI_API_KEY` there works with the default model `gpt-6-luna`.
- **Harness cache:** `benchmarks/runs/hallmark/.cache/cache.db` does not exist now. The old one was
  moved to `cache.db.pre-0.20` because the cache-slot bug had polluted its by-id entries. The next
  audit starts a fresh cache. Delete the old file once the run is done; nothing reads it.
- `.cf-studio/.core` and `.gen` were restored with `uv run cfs init --yes`. That command also
  rewrites tracked files (`.cf-studio/version.toml`, `whatsnew.toml`, `config/README.md`,
  `.gitignore`) and recreates `CLAUDE.md`, which the user deliberately deleted in 9c828c4. Revert
  both if it is ever re-run.

## Evidence so far

- **40-entry smoke, before this session's fixes.** On the API backend at pipeline 0.17: 6
  unresolved, all DBLP errors. On ClickHouse at 0.19: 32 `exactly_one`, 7 `none`, 1 `multiple`, in
  31 s.
- **10-entry smoke at 0.20** (`--limit 10 --seed 1`, ClickHouse): 9 `exactly_one`, 1 `none`, in
  18 s, with no errors.
- **5 `fabricated_doi` entries at 0.20:**
  - 4 matched a real work; all 4 now get a `doi` error (404 at doi.org), so `strict` scores them
    HALLUCINATED and `identity` VALID;
  - the 5th was `none`.
- **20 VALID entries with a published DOI:**
  - 18 have no finding that `strict` acts on;
  - `d0f7f9c72c33` (IBRNet) has a `doi` error, and the tool is right. The cited
    `10.1109/CVPR46437.2021.00469` belongs to "Delving into Localization Errors for Monocular 3D
    Object Detection"; IBRNet is `…00466`. **This is likely a HALLMARK label error**; list it as one
    in the report;
  - `a1de81b91af8` is a `strict` false positive. The cited author is `Francesco d&apos;Amore`, an
    HTML entity that is never decoded, so it fails the author check against `d'Amore` (see the
    gaps below).

## Next steps

All commands run from the repo root.

1. **Exercise `score` on a small run first**, since it has never been run:
   ```bash
   uv run python benchmarks/hallmark_bench.py audit --split dev_public --limit 40 --seed 0 \
       --out benchmarks/runs/hallmark/smoke40
   uv run python benchmarks/hallmark_bench.py score --split dev_public --out benchmarks/runs/hallmark/smoke40
   ```
   Then check `eval.*.txt` / `eval.*.json` and `summary.md`.
   - `score` restricts HALLMARK's data dir to the audited keys, so `--strict` works on a sample.
   - `build_summary` expects `eval.<mapping>.json` to hold `{"conservative": …, "aggressive": …}`,
     which is what `--eval-mode both --output` writes, per `hallmark/cli.py`.
   - Fix whatever breaks, with a test.
2. **Run the full split.** It should take minutes on ClickHouse; use `run_in_background` and monitor:
   ```bash
   uv run python benchmarks/hallmark_bench.py audit --split dev_public > benchmarks/runs/hallmark/dev_public.log 2>&1
   uv run python benchmarks/hallmark_bench.py score --split dev_public
   ```
   The default output directory is `benchmarks/runs/hallmark/dev_public`. A run directory refuses to
   resume under a different `pipeline_version`, so any further verdict-affecting fix means a fresh
   `--out`, or deleting the old one.
3. **Report to the user:**
   - the metrics table (identity/strict × conservative/aggressive), with coverage next to it;
   - per-type detection rates;
   - the false-positive list, separating likely label errors such as IBRNet;
   - the misses by type;
   - the unresolved entries and their reasons;
   - the tool gaps the run exposes.

   Then mark the acceptance criterion in `architecture/features/identification.md` §6 ("Verdict
   accuracy is measured on the HALLMARK `dev_public` split…") as `[x]`.
4. Optional, if the user wants it: `test_public` (831 entries), or an API-backend run for
   comparison. The API run is about 1 h, because Semantic Scholar is limited to 1 request/s.

## Known remaining gaps (not fixed; raise with the user if the run shows they matter)

- **HTML entities in author names are not decoded.** `d&apos;Amore` fails the author check against
  `d'Amore`, which is a `strict` false positive. Titles are already unescaped for search
  (`titlewords.py`), but authors are not.
- **The matched DOI shown can be the arXiv DataCite DOI** even when the pooled artifact also holds
  the published DOI. `_merge_ids` keeps the richest record's DOI. The DOI check is unaffected (it
  reads `merged_dois`); the "matched: doi:…" line and the harness's `matched_doi` are.
- **ClickHouse title search still misses a title with two wrong words.** The APIs are more
  forgiving.
- **The DBLP dump in ClickHouse has no landing page (`ee`) or pages.** This is a data limitation of
  the mirror; the SPARQL backend has both.
- **A cold S2 title search takes up to about 2 s** (a per-part text index over a 198 M-row part).
  Accepted; a finer index granularity could help.
- **The coverage caveat is year-level**, because entries carry no publication date.

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
  prerequisites apply: a phase plan, a DoD and acceptance criteria. Writing them yourself (e.g. in
  the scratchpad) satisfies them.
- **Never send the user's email address to an external service.**
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
  - read-only probe scripts against ClickHouse can use this project's venv
    (`.venv/bin/python`, `AuditConfig()` reads `.env`).
- `parse_bib` returns three values: `entries, twins, unparsed`.
