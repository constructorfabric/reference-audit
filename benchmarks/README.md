# Benchmarking on HALLMARK

[HALLMARK](https://github.com/rpatrik96/hallmark) is a citation-hallucination benchmark. Each split is a
set of standalone BibTeX entries labelled `VALID` or `HALLUCINATED`, spanning 14 hallucination types in
3 difficulty tiers. `benchmarks/hallmark_bench.py` runs the full pipeline (network + LLM) on a split and
scores it with HALLMARK's own evaluator.

The pytest regression corpus is independent of this external-evaluator setup: both public v1.2
splits are copied under [`tests/fixtures/hallmark/v1.2`](../tests/fixtures/hallmark/v1.2/README.md),
with the MIT license, author credit, pinned provenance and separate evidence-backed label reviews.
It has offline parsing/source regressions and opt-in live audits, and needs no HALLMARK installation.
The tests use native identity/metadata expectations and accept explicitly reported uncertainty;
binary labels, twelve proposed corrections and a historical-title dispute are diagnostic only.
This harness continues to report metrics against unchanged upstream labels.

HALLMARK gets its own checkout and venv (`--hallmark-dir`, default `benchmarks/.hallmark`). `score` and
`submit` call its CLI (`--hallmark-bin`, default `<hallmark-dir>/.venv/bin/hallmark`); the harness never
imports it. Every subcommand takes `--split` (default `dev_public`) and `--out`, the run directory
(default `benchmarks/runs/hallmark/<split>`). All commands here run from the repository root. Pin the
commit so results stay reproducible:

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
  [Local ClickHouse backend](../docs/clickhouse-backend.md)).
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

A native `contradiction` (resolved paper ID with conflicting citation fields) maps to
HALLUCINATED in `strict` and UNCERTAIN in `identity`. It retains the resolved target and evidence;
prior stored submissions remain historical results from their recorded pipeline version.

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
[`hallmark_label_errors.md`](hallmark_label_errors.md).

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

## Running on `test_hidden`

HALLMARK withholds `test_hidden` (453 entries, plus any canary) to keep its evaluation honest, so
neither its entries nor its labels are in the pinned checkout. HALLMARK's `HOSTING.md` says the
access procedure will be published at camera-ready, along with the leaderboard submission
instructions. reference-audit has not been run on this split, so there are no results for it.

What the harness can do with it is `audit` and `submit`, since neither reads labels. `score` cannot
run on this split. The labels are withheld, and HALLMARK's loader reads `test_hidden` from
`<data-dir>/v1.2/hidden/test_hidden.jsonl`, while `score` writes the labels it evaluates against to
`<data-dir>/v1.2/<split>.jsonl`. So even with the labels, `hallmark evaluate` would not find them.
HALLMARK scores the split itself.

1. **Put the blind split where `audit` reads it.** That is
   `benchmarks/.hallmark/data/v1.2/test_hidden_blind.jsonl`, with one record per line in the shape
   of the public blind files (`bibtex_key`, `bibtex_type`, `fields`, `raw_bibtex`). If HALLMARK gives
   you the labelled file instead, strip it to that shape and keep the labelled file outside the
   checkout:

   ```bash
   jq -c '{bibtex_key, bibtex_type, fields, raw_bibtex}' /path/to/test_hidden.jsonl \
     > benchmarks/.hallmark/data/v1.2/test_hidden_blind.jsonl
   ```

   The manifest records this file's SHA-256. If you made the file yourself, that is the hash of
   your copy, not of a file HALLMARK published.

2. **Audit the whole split** with the settings the committed submissions use. Do not pass `--limit`,
   because `submit` refuses a sample:

   ```bash
   uv run python benchmarks/hallmark_bench.py audit --split test_hidden \
     --partial-authors error --backend clickhouse   # or --backend api
   ```

   It needs an LLM key, the same as any other split. If it is interrupted, re-run the same command
   and it resumes from the last finished chunk. For timing, `test_public`'s 831 entries took 21
   minutes on ClickHouse.

3. **Check the run before submitting.** `benchmarks/runs/hallmark/test_hidden/run.json` gives the
   outcome counts before and after the retry. Entries that failed the `.bib` round-trip, or whose
   audit raised, are submitted as `UNCERTAIN` with `evaluated=false`. They are not given a guessed
   label. If many entries are failed or unresolved, a source or the LLM was probably down during the
   run. Once it is back, re-run the same `audit` command with a higher `--retry-unresolved`, for
   example `2`. Finished chunks are reused, so only the entries still unresolved or failed are
   audited again.

4. **Write the submission:**

   ```bash
   uv run python benchmarks/hallmark_bench.py submit --split test_hidden \
     --dest benchmarks/runs/hallmark/test_hidden/submission
   ```

   This writes `reference-audit-strict_test_hidden_predictions.jsonl` and
   `reference-audit-strict_test_hidden.json`, after `hallmark validate-predictions` has accepted
   the predictions. Add `--mapping identity` for the second mapping.

   `--dest` matters here. By default the files go to `benchmarks/submissions/hallmark/`, which is
   committed. Each prediction's `reason` quotes the matched record and the field findings, so the
   file gives away part of the hidden split. Keep it under the gitignored `benchmarks/runs/`, and
   check HALLMARK's terms before committing or publishing it.

5. **Send the predictions file and its manifest to HALLMARK** by the procedure it publishes.
   Until then, HALLMARK's CLI points to <https://github.com/rpatrik96/hallmark>.

## Verdict-to-label mappings

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
on the `doi` field (see [How it works](../docs/how-it-works.md)). `strict` therefore counts it as HALLUCINATED;
`identity` does not, since a real document still matches.

## Result: `dev_public`, pipeline 0.23

The results below, and the committed submissions, were measured at pipeline 0.23. The current
pipeline is 0.27, and it has not been re-measured with this evaluator:
- 0.24: bibtexparser 2. `.bib` values are read by the tool itself and LaTeX is decoded with
  pylatexenc, which changes some field values and which entries are unparseable.
- 0.25: HTML character references in source values are decoded, so a source title with `&amp;` is
  settled by rule instead of going to the field-check LLM.
- 0.26: a cited OpenAlex Work id is looked up even when the entry also has a DOI or arXiv id, and a
  Work id that is another work, resolves to nothing or could not be looked up is reported.
- 0.27: venue contradictions require evidence bound to the cited published artifact or a complete,
  explicitly scoped catalogue. A different version's venue alone stays unverified. Consequently the
  historical strict metrics and label-error proposals are not current field-check expectations.

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

## Result: `test_public` and `stress_test`, pipeline 0.23

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
