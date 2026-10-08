"""Runtime configuration for the reference auditor.

All settings load from the environment / `.env` (pydantic-settings). Secrets live only in
`.env` (git-ignored); never hard-code keys. The LLM model defaults to `gpt-6-luna` per
the README but is configurable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
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
    model: str = "gpt-6-luna"
    # None ⇒ omit `temperature` and use the model default. gpt-6-luna rejects any value but its default
    # (1), so a fixed 0.0 would fail every call; set LLM_TEMPERATURE=0 for models that accept it.
    llm_temperature: float | None = None
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
    openlibrary_email: str | None = Field(default=None, alias="OPENLIBRARY_EMAIL")
    google_books_api_key: str | None = Field(default=None, alias="GOOGLE_BOOKS_API_KEY")

    # --- Source backend: where Semantic Scholar, OpenAlex and DBLP are read from ---
    # "api": their public APIs (rate-limited; needs no setup). "clickhouse": a local ClickHouse mirror
    # of the three databases (no rate limits; must carry the full-text title indexes, see README).
    # Crossref, arXiv, Open Library, Google Books and the web/publisher fetches are API-only.
    source_backend: Literal["api", "clickhouse"] = Field(
        default="api", validation_alias=AliasChoices("SOURCE_BACKEND", "source_backend")
    )
    clickhouse_host: str = Field(default="127.0.0.1", alias="CLICKHOUSE_HOST")
    clickhouse_port: int = Field(default=8123, alias="CLICKHOUSE_PORT")
    clickhouse_user: str = Field(default="default", alias="CLICKHOUSE_USER")
    clickhouse_password: str = Field(
        default="",
        validation_alias=AliasChoices("CLICKHOUSE_DEFAULT_USER_PASSWORD", "CLICKHOUSE_PASSWORD"),
    )
    clickhouse_s2_db: str = Field(
        default="s2ag", validation_alias=AliasChoices("CLICKHOUSE_S2_DB", "CLICKHOUSE_DB")
    )
    clickhouse_openalex_db: str = Field(default="openalex", alias="CLICKHOUSE_OPENALEX_DB")
    clickhouse_dblp_db: str = Field(default="kb", alias="CLICKHOUSE_DBLP_DB")
    clickhouse_concurrency: int = 8         # queries in flight against the local server
    clickhouse_query_timeout: float = 60.0  # seconds; a slower query is an error, never "not found"

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

    # --- Citation alignment (advisory; opt-in) ---
    # Compare each citing context against the cited work's abstract. Off by default: it needs the LLM
    # and an abstract, and adds a per-citation cost. CLI --check-citations enables it.
    check_alignment: bool = False

    # --- Web artifacts (URL-only @misc): HTML-metadata check before the LLM fallback ---
    web_title_accept: float = 0.85        # page meta-title vs cited-title floor for a deterministic confirm

    # --- Web artifacts: headless rendering of client-side-rendered (single-page-app) pages ---
    web_render_enabled: bool = True       # render SPA shells in a headless browser before judging
    web_render_browser_path: str | None = Field(default=None, alias="WEB_RENDER_BROWSER_PATH")
    web_render_timeout: float = 30.0      # seconds before a render is treated as a (retryable) error
    web_render_virtual_time_ms: int = 15000  # JS virtual-time budget given to the headless browser

    # --- PDF input (GROBID) ---
    # The service is NOT managed by this package. Point this at a running instance; a PDF input with
    # no reachable GROBID is a reported failure, never a silently empty reference list.
    #   podman run -d --name grobid -p 8070:8070 docker.io/grobid/grobid:0.8.2.1-crf
    # Intentionally NOT declared with alias="GROBID_URL": a non-aliased field already reads its
    # upper-cased env var, whereas an aliased one silently ignores by-name construction here
    # (`populate_by_name` is not set), which would make AuditConfig(grobid_url=...) a no-op in tests.
    grobid_url: str = "http://localhost:8070"

    # --- Cache / pipeline ---
    cache_path: Path | None = None        # default: <bib_dir>/.reference_audit/cache.db
    # Bump when thresholds/prompts/rules change, or when a new verdict-producing path lands.
    # 0.16: PDF input via GROBID — TEI entry-type inference changes source routing, and an entry with
    #       neither a title nor an identifier is now reported unresolved instead of being searched on
    #       (a title-less query could previously score an arbitrary paper as a match).
    # 0.17: default LLM model gpt-5.4-mini → gpt-6-luna, and temperature is no longer pinned to 0.0
    #       (model default unless LLM_TEMPERATURE is set) — adjudication behavior changes.
    # 0.18: DBLP is queried through its SPARQL endpoint (QLever word search, shortest title first)
    #       instead of the search API, which now serves automated clients a bot-challenge page. DBLP's
    #       candidates and their ranking change, and DBLP no longer errors on every query, so entries
    #       that were left unresolved can now reach a verdict.
    # 0.19: a second source backend — Semantic Scholar, OpenAlex and DBLP read from a local ClickHouse
    #       mirror (full-text title index, all title words required, shortest title first). The
    #       verdict cache is now also keyed by backend.
    # 0.20: the local ClickHouse title search retries with each word left out when no all-words hit is
    #       a near-exact title, and OpenAlex rows gain their best open-access location as a version
    #       link (candidates change). A cited DOI the matched work does not carry is checked and
    #       reported as a `doi` field finding; an unresolved entry records why; a `none` on a local
    #       snapshot that may predate the work carries a coverage caveat.
    # 0.21: HTML character references (`d&apos;Amore`, `&amp;`) are decoded in .bib fields and in
    #       author/title normalization, so author checks and title scores change for such entries.
    pipeline_version: str = "0.21"

    def llm_enabled(self) -> bool:
        return self.use_llm and bool(self.openai_api_key)

    def resolved_mailto(self) -> str:
        """Crossref polite-pool contact; fall back to the Unpaywall email if set."""
        if self.crossref_mailto and self.crossref_mailto != "reference-audit@example.org":
            return self.crossref_mailto
        return self.unpaywall_email or self.crossref_mailto
