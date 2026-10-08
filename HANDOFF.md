# Handoff: reference-audit on the HALLMARK benchmark

Updated 2026-10-09, for the next session to continue from a fresh context.
Branch `feat/hallmark-clickhouse`. Commits up to `c331de5` are pushed. Everything after it (the tool-gap
fixes, the entity fix, this session's harness/limiter fixes and the HANDOFF updates) is **local only**:
push when the user agrees. No PR is open; `main` is unchanged.

## Goal

Measure reference-audit on [HALLMARK](https://github.com/rpatrik96/hallmark) `dev_public` (1,119
entries: 513 VALID, 606 HALLUCINATED). The user wants:
- leaderboard-comparable metrics;
- per-type detection rates;
- a work list of false positives and misses.

## Status: measured at pipeline 0.21; the work list is diagnosed, nothing on it is fixed

The run is in `benchmarks/runs/hallmark/dev_public/` (gitignored): `summary.md`, `eval.*.json`,
`predictions.*.jsonl`, `audits.jsonl`. The headline table is in README, "Benchmarking on HALLMARK →
Result". In brief:

| mapping | mode | DR | FPR | F1 | MCC | coverage |
| --- | --- | --- | --- | --- | --- | --- |
| identity | conservative | 0.391 | 0.002 | 0.561 | 0.481 | 0.954 |
| strict | conservative | 0.949 | 0.094 | 0.943 | 0.859 | 0.747 |
| strict | aggressive | 0.958 | 0.392 | 0.837 | 0.615 | 0.747 |

- 7 entries are not evaluated (`.bib` round-trip).
- 41 HALLUCINATED entries are unresolved:
  - 33 on arXiv 429s; **this IP was throttled by arXiv**, which answered 429 even to a single request;
  - 8 at `llm_max_candidates=8`.
- The audit wall time was about 36 min (31 for the run, 5 for a second retry). `run.json` says 5 min,
  because of a harness bug that is now fixed.

This session's commits:
- `361f533` `score`: relabelled rows without type keys crashed it; the summary now shows coverage and
  relabel history.
- `2b0cec1` Per-source in-flight cap (Crossref 3, arXiv 1), and every retry goes through the limiter.
  Before this, 21–25% of a chunk came back unresolved on Crossref 429s.
- `b004463` arXiv is spaced at 1 request / 3 s, per its API terms.
- `4e34b1d` `score` dropped HALLMARK's `--strict`, which rejects `evaluated=false`. Adds an
  end-to-end `score` test through HALLMARK's binary.
- The next commit: run wall time accumulates over resumed invocations; README result section; the
  acceptance criterion in `identification.md` §6 is ticked.

None of these is verdict-affecting: `pipeline_version` stays 0.21.

## Work list (diagnosed with evidence; each fix below is verdict-affecting → bump to 0.22)

Counts are from the 0.21 run. A `strict` FP is a VALID entry the `strict` mapping calls
HALLUCINATED: 32 in all, of which 24 are tool errors, 7 are likely label errors and 1 is a typo.

1. **The pooled record's venue, year and title come from the preprint copy.**
   - Cause: `matching/pool.py` `_representative`. `_FIELD_SOURCE_PRIORITY` has no `dblp`; OpenAlex's
     arXiv DataCite member (venue `arXiv (Cornell University)`, preprint year) outranks DBLP's `ICLR`;
     and the title is the richest member's.
   - Effect: venue `unverifiable` on **168 of 513 VALID** (strict UNCERTAIN), about 60 HALLUCINATED
     made UNCERTAIN, and 8 FPs (6 year, 2 title: `d541bf3fa5b9`, `be850b9b2b71`).
   - Fix: compile venue, year and title from published (non-preprint-ish) members first, add `dblp`
     to the priority, and treat `CoRR` and `Infoscience` as repository venues
     (`fieldcheck._REPOSITORY_VENUE_RE`).
2. **The title-check LLM is told the .bib entry is the confirmed work.**
   - Cause: `llm/prompts.py:108` `field_check_user` puts `entry.title` under
     "CONTEXT — the same work, confirmed by identifier".
   - Effect: the LLM rules "Resilient" vs "Robust", and two entirely different titles, as formatting
     or uncertain. That is 8 strict misses (5 `near_miss_title`, 3 `chimeric_title`).
   - Fix: take the context from the matched record. Fix item 1 first, or published-vs-preprint title
     variants will become FPs.
3. **The author check (`matching/names.py` `mismatched_authors`).**
   - It is surname-only, against the best record only. That gives 12 FPs:
     - given/family order swapped (`Li Tian`, `Jing Li`, `Yu Zheng`, `Moriano Pablo`);
     - compound surnames (`Sestorain Saralegui`, `Riquelme Ruiz`, `Fernández García`, `Gontijo-Lopes`);
     - defective source records (OpenAlex `Ed H.`, S2 `Wenhan Wang`, S2 missing Osher).
   - Fuzzy surname 0.8 lets a fabricated `Carreira` pass as `Barreira` (Flamingo).
   - The "record ≥ 0.9 × cited length" guard skips the check when the authors were replaced
     wholesale: 3 misses (`b9e0c641d08e`, `d453d206147d`, `b8d6a54dae43`).
   - Fix: compare names order-insensitively; flag only an author absent from every member record;
     replace the length guard with "the leading authors agree".
4. **Pooling over-merges a paper with its journal extension.**
   - CrossFormer (ICLR 2022) and CrossFormer++ (TPAMI) fuse transitively through their preprints.
     The ICLR record has no DOI, so the V1 veto never fires, and the representative's
     CrossFormer++ title and authors hide the match. That is the only `identity` FP (`f8d361220ec8`).
   - Related: the canonical venue and year come from a later version, giving 3 FPs:
     - `b683f8f34292`: ICML vs a 2025 journal;
     - `f7a0b6460bd6`: NeurIPS vs IEEE TIT;
     - `d1e149ca3cc4`: NeurIPS vs IJCAI.
   - Fix: check the cited venue and year against every version, and do not bridge two published
     records through a preprint when their author sets differ.
5. **doi.org returns HTTP 500 for an unregistered prefix** (e.g. `10.8888/...`), which is read as
   "unreachable" → `doi` unverifiable. That is 8 `fabricated_doi` UNCERTAIN. The Handle API
   `https://doi.org/api/handles/<doi>` is authoritative (`responseCode` 1 = registered, 100 = not
   found, HTTP 404). Use it in `publisher.doi_registered`.
6. Not diagnosed: 3 `fabricated_doi` → `multiple` (`abd68711ff28`, `cf9b91805136`, `d0a040eb49c2`).
7. Minor and operational:
   - the arXiv base URL is `http://`, so every query pays a 301 → https;
   - a resolved verdict is cached even when a source errored, with a thinner artifact and no record
     of the error;
   - the 8 LLM-cap entries are unresolved by design (raise `llm_max_candidates`? user's call).

**Likely HALLMARK label errors** (labelled VALID; the tool flags them with evidence). All but OPT
were relabelled HALLUCINATED → VALID by HALLMARK:
- the cited DOI belongs to another paper: `d0f7f9c72c33` IBRNet, `f802800935ef` ImageBind,
  `ff2931c3228f` MoCo v3, `ded9f5844e90` TensoRF;
- authors not on the paper: `a24129d1c5e5` Flamingo (4), `e9e08922a057` PaLM (4, also cited as
  ICML), `c65faf378a95` OPT (2, also cited as ACL).

**Policy questions for the user** (disagreements with HALLMARK by design, not bugs):
- `partial_author_list`: the tool does not report omitted authors (11 misses, 14 UNCERTAIN).
- Hyphen-only `near_miss_title` (`cc3bac858db2`, `1cc022db3273`) are treated as formatting.
- A duplicated "in the in the" in a VALID title (`e73343fc5b98`) is flagged as a title error.

## Next steps (user decides)

1. When arXiv lifts its throttle (check: `curl -sL "https://export.arxiv.org/api/query?id_list=2203.05104"`
   returns 200), rerun `audit --split dev_public --retry-unresolved 3`, then `score`.
2. Fix work-list items 1–5 under one `pipeline_version` bump (0.22). Re-run dev_public in a fresh
   `--out` (about 35 min), compare with 0.21, then update the README result.
3. Optional: `test_public` (831), an API-backend run, and push / open a PR.

## Machine state outside git (this machine only)

- `benchmarks/.hallmark/`: HALLMARK at `f774fa40675daa83eca6201637a94c4536b7bb3e`, with its own venv.
  Labelled split: `data/v1.2/dev_public.jsonl`; the harness audits `dev_public_blind.jsonl`.
- `.env`: `SOURCE_BACKEND=clickhouse`, ClickHouse credentials, `OPENAI_API_KEY` (model `gpt-6-luna`).
  The polite-pool mailto comes from `PAPER_SEARCH_MCP_UNPAYWALL_EMAIL` (set by the user).
- `benchmarks/runs/hallmark/.cache/cache.db` is the cache of the 0.21 run. Delete these once no
  longer needed; nothing reads them:
  - `cache.db.pre-0.20` (polluted by the old cache-slot bug);
  - `cache.db.throttled-0.21{,-wal,-shm}` (the run stopped for Crossref 429s);
  - `benchmarks/runs/hallmark/dev_public-throttled/`.
- `benchmarks/runs/hallmark/smoke40/`: the 40-entry smoke run.
- Running `uv run cfs init --yes` rewrites tracked files and recreates `CLAUDE.md`, which the user
  deleted on purpose in 9c828c4. Revert both if it is ever re-run.

## Decisions the user made (do not re-ask)

- `dev_public` first; the harness lives in the repo under `benchmarks/`, with docs and `cfs` updates.
- DBLP via SPARQL; the backend is a per-run switch (`api` default, `.env` selects `clickhouse`).
- I may add indexes to the local ClickHouse.

## Rules and gotchas

- AGENTS.md applies:
  - bump `AuditConfig.pipeline_version` on any verdict-affecting change;
  - update README and `architecture/` in the same change, and keep `uv run cfs validate` green;
  - a failure is reported, never defaulted.
- Before editing governed docs, the `cf-studio` prerequisites apply (a phase plan, a DoD and
  acceptance criteria); writing them in the scratchpad satisfies them.
- **Never send the user's email address to an external service.**
- HALLMARK `evaluate`:
  - conservative *excludes* UNCERTAIN, and aggressive counts it as HALLUCINATED;
  - `evaluated=false` is excluded from both;
  - `--strict` rejects `evaluated=false`, so the harness does not use it.

  Always report coverage next to the metrics.
- HALLMARK v1.2 rows relabelled HALLUCINATED → VALID carry no `hallucination_type` /
  `difficulty_tier` key.
- `clickhouse-connect`:
  - use the sync client with `autogenerate_session_id=False`;
  - search words must be lower-case;
  - read-only probes can use `.venv/bin/python` (`AuditConfig()` reads `.env`).
- Per process, Crossref allows 3 requests in flight and arXiv 1. **Do not run a probe in parallel with
  a benchmark run**: each process gets its own cap, so a probe doubles the load on the source.
- When moving a SQLite cache aside, move its `-wal` / `-shm` files with it.
