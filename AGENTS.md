<!-- @cf:root-agents -->
```toml
cf-studio-path = ".cf-studio"
```

ALWAYS resolve and enforce prerequisites of skills/workflows/commands BEFORE applying user intent.
<!-- /@cf:root-agents -->

The ovreall goal of the project is to provide the level of reliability suitable for mass automated processing. This means that failures shouldn't be handled silently (e. g. by returning null data). This means a two level approach:
1. On the level of individual record, if there are failures, they should be logged and reported. Don't apply defaults or incomplete guesses - an outcome `we could not check the reference for reasons A, B, and C" is actually desirable.
2. Records are processed independently - if one record fails, it shouldn't affect the processing of other records.

When querying the LLMs for structured output, always use pydantic models.

The whole-entry verdict cache (`entry_verdict_cache`) is gated only on `(pipeline_version, model)` (and kept per source backend, `api` / `clickhouse`). Any change that can alter a verdict — thresholds, prompts, matching/scoring rules, or a *new
verdict-producing path* (e.g. a new source or override step) — MUST bump `AuditConfig.pipeline_version` in the same change. Forgetting this serves stale verdicts from before
the change: pre-feature entries keep their old verdict and never run the new code, which can produce self-contradictory reports (e.g. a freshly-recomputed match note next to a cached "no match").

Documentation organization:
1. README.md should be the front page on GitHub. It should state briefly what the project does, provide quick start instructions, and link to the full documentation.
2. docs/*.md should contain the current documentation
3. docs/archive contains the results of past experiments. They don't need to be updated; the code doesn't need to be backward-compatiable with them
4. docs/ideas contains the ideas which haven't been tried yet 

Every change to the code must be reflected in the documentation as part of the same change:
- Update `README.md` and docs/ whenever behavior, CLI options, sources, output format, or project layout change.
- Update the governed `cfs` docs under `architecture/` (SPEC, PRD, DESIGN, DECOMPOSITION, features)
  to match, and keep `cfs validate` passing. When a capability is implemented but not yet
  `@cpt`-traced to code, say so explicitly rather than marking it done.
