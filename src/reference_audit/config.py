"""Runtime configuration for the reference auditor.

All settings load from the environment / `.env` (pydantic-settings). Secrets live only in
`.env` (git-ignored); never hard-code keys. The LLM model defaults to `gpt-5.4-mini` per
the README but is configurable.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuditConfig(BaseSettings):
    """Central config: LLM, data-source credentials, matching thresholds, cache."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
    )

    # --- LLM (OpenAI SDK) ---
    model: str = "gpt-5.4-mini"
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")

    # --- Data-source credentials (all already present in .env) ---
    crossref_mailto: str = Field(
        default="reference-audit@example.org", alias="CROSSREF_MAILTO"
    )
    s2_api_key: str | None = Field(default=None, alias="S2_API_KEY")
    ncbi_api_key: str | None = Field(default=None, alias="NCBI_API_KEY")
    nasa_ads_api_key: str | None = Field(default=None, alias="NASA_ADS_API_KEY")
    core_api_key: str | None = Field(default=None, alias="CORE_API_KEY")
    unpaywall_email: str | None = Field(
        default=None, alias="PAPER_SEARCH_MCP_UNPAYWALL_EMAIL"
    )

    # --- Matching thresholds (calibrated against the pilot; see plan risk #3) ---
    title_accept: float = 0.92            # auto_accept title floor (entry has an identifier)
    title_backfill: float = 0.95          # stricter title floor when entry has NO identifier
    author_accept: float = 0.80           # auto_accept author-overlap floor
    composite_reject: float = 0.40        # auto_reject ceiling
    prefix_trap_tail_jaccard: float = 0.34  # V3: tail-token Jaccard below this ⇒ distinct
    author_set_distinct_jaccard: float = 0.60  # V4: author-set Jaccard below this ⇒ distinct

    # --- LLM adjudication ---
    use_llm: bool = True                  # CLI --no-llm sets this False (deterministic CI)
    llm_concurrency: int = 8
    llm_max_candidates: int = 8           # cap CAN_CORRESPOND calls per entry (cost control)

    # --- Step 3: field correctness ---
    check_fields: bool = True             # verify each field of an exactly-one match is correct

    # --- Web artifacts (URL-only @misc): HTML-metadata check before the LLM fallback ---
    web_title_accept: float = 0.85        # page meta-title vs cited-title floor for a deterministic confirm

    # --- Cache / pipeline ---
    cache_path: Path | None = None        # default: <bib_dir>/.reference_audit/cache.db
    pipeline_version: str = "0.10"        # bump when thresholds/prompts/rules change

    def llm_enabled(self) -> bool:
        return self.use_llm and bool(self.openai_api_key)

    def resolved_mailto(self) -> str:
        """Crossref polite-pool contact; fall back to the Unpaywall email if set."""
        if self.crossref_mailto and self.crossref_mailto != "reference-audit@example.org":
            return self.crossref_mailto
        return self.unpaywall_email or self.crossref_mailto
