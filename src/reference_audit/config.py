"""Runtime configuration for the reference auditor.

Settings load (pydantic-settings) from, in decreasing priority: constructor arguments, environment
variables, `.env` in the current directory, and the per-user TOML file `user_config_file()`
(`~/.config/reference-audit/config.toml`). Secrets live only in those files or the environment
(the project `.env` is git-ignored); never hard-code keys. The LLM model defaults to `gpt-6-luna`
per the README but is configurable.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


class UserConfigError(ValueError):
    """The per-user config file exists but cannot be used: unreadable, not TOML, or unknown keys."""


def user_config_file() -> Path | None:
    """The per-user config file, so an installed package finds its settings from any directory.

    `$XDG_CONFIG_HOME/reference-audit/config.toml`, defaulting to `~/.config/reference-audit/
    config.toml`; None when no home directory can be determined. Resolved on every `AuditConfig()`.
    """
    base = os.environ.get("XDG_CONFIG_HOME")
    if not base or not Path(base).is_absolute():  # the XDG spec says to ignore a relative value
        try:
            base = Path.home() / ".config"
        except RuntimeError:
            return None
    return Path(base) / "reference-audit" / "config.toml"


def _input_key(name: str, field: FieldInfo) -> str:
    """The key validation reads `field` from: its (first) alias, else its name."""
    alias = field.validation_alias or field.alias
    if isinstance(alias, AliasChoices):
        alias = alias.choices[0]
    return alias if isinstance(alias, str) else name


class _UserConfigSource(InitSettingsSource):
    """`user_config_file()`, keyed by `AuditConfig` field name (`openai_api_key = "..."`).

    Unlike the environment and `.env`, where any unrelated variable may appear, this file belongs to
    the tool, so an unknown key (a typo that would otherwise silently leave a setting unset) is an
    error. A missing file is skipped.
    """

    def __init__(self, settings_cls: type[BaseSettings], path: Path | None):
        data: dict[str, Any] = {}
        if path is not None and path.exists():
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
                raise UserConfigError(f"cannot read config file {path}: {exc}") from exc
        fields = settings_cls.model_fields
        unknown = sorted(set(data) - set(fields))
        if unknown:
            raise UserConfigError(
                f"unknown setting(s) in {path}: {', '.join(unknown)}. Keys are the lower-case "
                "AuditConfig field names (e.g. openai_api_key, not OPENAI_API_KEY)."
            )
        super().__init__(settings_cls, {_input_key(k, fields[k]): v for k, v in data.items()})


class AuditConfig(BaseSettings):
    """Central config: LLM, data-source credentials, matching thresholds, cache."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Highest priority first: the per-user file is the fallback beneath the environment and ./.env.
        return (init_settings, env_settings, dotenv_settings, file_secret_settings,
                _UserConfigSource(settings_cls, user_config_file()))

    # --- LLM (OpenAI SDK) ---
    model: str = "gpt-6-luna"
    # None ⇒ omit `temperature` and use the model default. gpt-6-luna rejects any value but its default
    # (1), so a fixed 0.0 would fail every call; set LLM_TEMPERATURE=0 for models that accept it.
    llm_temperature: float | None = None
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")

    # --- Data-source credentials ---
    crossref_mailto: str = Field(
        default="reference-audit@example.org", alias="CROSSREF_MAILTO"
    )
    s2_api_key: str | None = Field(default=None, alias="S2_API_KEY")
    openalex_api_key: str | None = Field(default=None, alias="OPENALEX_API_KEY")
    unpaywall_email: str | None = Field(
        default=None, alias="PAPER_SEARCH_MCP_UNPAYWALL_EMAIL"
    )
    openlibrary_email: str | None = Field(default=None, alias="OPENLIBRARY_EMAIL")
    google_books_api_key: str | None = Field(default=None, alias="GOOGLE_BOOKS_API_KEY")

    # --- Source backend: where Semantic Scholar, OpenAlex and DBLP are read from ---
    # "api": their public APIs (rate-limited; needs no setup). "clickhouse": a local ClickHouse mirror
    # of the three databases (no rate limits; must carry the full-text title indexes, see docs/clickhouse-backend.md).
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
    # A cited author list that names only some of the work's authors without saying so ('and
    # others'): `ignore` it, `warn` (an `uncertain` author finding) or report an `error`. Many
    # bibliographies shorten long lists; HALLMARK counts an unmarked omission as a hallucination.
    # CLI --partial-authors.
    partial_authors: Literal["ignore", "warn", "error"] = Field(
        default="warn", alias="PARTIAL_AUTHORS"
    )

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

    # --- PDF input ---
    # Who reads the reference list of a PDF that embeds no BibTeX records (CLI --pdf-extractor).
    # "llm": `pdf_extraction_model` reads the PDF (text layer and page images) through the OpenAI API;
    #   a list that fails its checks, and entries the audit flags, are re-read by
    #   `pdf_escalation_model` (see `pdf.llm_refs`). Gives no in-text citations, so citedness is
    #   unknown and --check-citations is unchecked. Sends the PDF to the configured OpenAI endpoint.
    # "grobid": the operator's GROBID service at `grobid_url`, which also links in-text citations.
    pdf_extractor: Literal["llm", "grobid"] = "llm"
    pdf_extraction_model: str = "gpt-6-luna"
    pdf_escalation_model: str = "gpt-6.1-sol"
    # GROBID, when pdf_extractor = "grobid". The service is NOT managed by this package. Point this at a running instance; a PDF input with
    # no reachable GROBID is a reported failure, never a silently empty reference list.
    #   podman run -d --replace --name grobid -p 8070:8070 docker.io/grobid/grobid:0.9.1-crf
    # Intentionally NOT declared with alias="GROBID_URL": a non-aliased field already reads its
    # upper-cased env var, whereas an aliased one silently ignores by-name construction here
    # (`populate_by_name` is not set), which would make AuditConfig(grobid_url=...) a no-op in tests.
    grobid_url: str = "http://localhost:8070"
    # A PDF that embeds the BibTeX records of its cited works (`pdf.embedded`, e.g. one made with the
    # AI4X template) is audited from them, without GROBID. CLI --ignore-embedded turns this off.
    use_embedded_references: bool = True

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
    # 0.22: the HALLMARK run's work list. Field checks compare against the version the entry cites
    #       (the pooled record keeps its member records); a pooled venue is never a preprint
    #       server while a member names the venue; pooling no longer fuses a paper with a journal
    #       extension that has other authors; the field-check LLM sees the matched work, not the
    #       entry, as context; authors are checked person by person against every source, as an
    #       `author` field finding (with the partial-list option); the cited DOI is checked with the
    #       doi.org Handle API, which answers 404 for an unregistered prefix.
    # 0.23: regressions the 0.22 run exposed. A field's canonical value is the one most sources of the
    #       cited version agree on (an OpenAlex title defect won on source rank); a pooled record's
    #       author list comes from the most reliable source, not the citation-richest (S2's defective
    #       list made the LLM reject a real paper); members are deduplicated; given-name forms
    #       ('Tim' / 'Timothy') and dropped middle initials match; the field-check prompt calls a
    #       different journal or conference series an error (the LLM hedged on ACL vs ICLR).
    # 0.24: bibtexparser 2. Field values are read by `parsing.bib` itself and LaTeX is decoded with
    #       pylatexenc (`parsing.latex`), so some values change: `\url{x}` is `x` (v1: `r̆lx`),
    #       `\textit{X}` is `X`, `\href{u}{X}` is `X <u>`. An undefined `@string`, a duplicate field or
    #       an undecodable value now makes only that entry unparsed (v1 failed the whole file on an
    #       undefined `@string` and kept the first of duplicate fields); commented twins are read
    #       from the uncommented text.
    # 0.25: HTML character references in source values are decoded (`SourceRecord`), as they already
    #       were in `.bib` values: a Crossref title "Allocation &amp; Regularization" no longer folds
    #       to the word "amp", so it is settled by rule instead of going to the field-check LLM, and
    #       the canonical value is reported decoded.
    # 0.26: a cited OpenAlex Work id is looked up on its own even when the entry also carries a DOI or
    #       arXiv id (OpenAlex was queried by those instead, so the Work id was never checked), and
    #       can then be pinned as the match; a cited DOI that is another Work is reported by the
    #       cited-DOI check. A cited Work id that resolves to a different work, to nothing, or could
    #       not be looked up is now reported as an issue, whatever the verdict.
    # 0.27: venue errors require evidence bound to the cited published artifact or an explicitly
    #       complete venue/year/track catalogue. Another version's venue or an LLM guess cannot
    #       establish non-publication. Venue findings retain the evidence's source and scope.
    # 0.29: native Word bibliography/CITATION input feeds the shared verdict-producing pipeline;
    #       invalidate whole-entry verdicts from before this input path.
    # 0.30: organizational authors ("AI4X Program Committee") are scored by their distinctive words
    #       and never match a person, a compound surname scores as its parts, and a URL in
    #       `howpublished`/`note` is read as the entry's URL — scoring and web routing change. The
    #       OpenAlex search no longer fails on titles containing `?` or `*`.
    # 0.31: PDF references are read by an LLM by default (`pdf.llm_refs`), and an entry whose audit
    #       ends in 'no match', a contradiction or an error on its title, authors, year or DOI is
    #       re-read by a stronger model and, if the reading differs, audited again — a new
    #       verdict-producing path. GROBID-read entries keep their text under `pdf_raw` /
    #       `pdf_ref_index` (were `grobid_raw` / `grobid_ref_index`).
    # 0.32: a cited NeurIPS proceedings URL's official BibTeX export is pooled into the matched work
    #       (a new verdict-artifact source: publisher, venue, DOI for 2022+); NeurIPS, ICLR, ICML,
    #       PMLR and JMLR join the no-DOI venue allowlist, whose "no DOI" warning is withdrawn when
    #       no published DOI is found; an arXiv DOI is no longer reported as a backfilled find for an
    #       entry citing that arXiv id or a no-DOI venue; enrichment now precedes the backfill note.
    pipeline_version: str = "0.32"

    def llm_enabled(self) -> bool:
        return self.use_llm and bool(self.openai_api_key)

    def resolved_mailto(self) -> str:
        """Crossref polite-pool contact; fall back to the Unpaywall email if set."""
        if self.crossref_mailto and self.crossref_mailto != "reference-audit@example.org":
            return self.crossref_mailto
        return self.unpaywall_email or self.crossref_mailto
