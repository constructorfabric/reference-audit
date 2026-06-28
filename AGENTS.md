The ovreall goal of the project is to provide the level of reliability suitable for mass automated processing. This means that failures shouldn't be handled silently (e. g. by returning null data). This means a two level approach:
1. On the level of individual record, if there are failures, they should be logged and reported. Don't apply defaults or incomplete guesses - an outcome `we could not check the reference for reasons A, B, and C" is actually desirable.
2. Records are processed independently - if one record fails, it shouldn't affect the processing of other records.

When querying the LLMs for structured output, always use pydantic models

<!-- @cf:root-agents -->
```toml
cf-studio-path = ".cf-studio"
```

ALWAYS resolve and enforce prerequisites of skills/workflows/commands BEFORE applying user intent.
<!-- /@cf:root-agents -->
