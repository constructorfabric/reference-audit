# Installation and configuration

## Requirements

- [`uv`](https://docs.astral.sh/uv/). It installs Python 3.14, which the tool needs, if your system
  has no suitable Python.
- Optional: an OpenAI API key (or a key for an OpenAI-compatible endpoint), for the LLM
  adjudication step. Without one, entries whose candidates the formal rules cannot settle are
  reported under `UNABLE TO VERIFY` with the reason, never guessed.
- Optional: a Chrome/Chromium binary, to render cited web pages that are JavaScript single-page apps
  (see [How it works](how-it-works.md)). Without one, such entries are left unresolved.
- **For PDF input only:** a running GROBID. It is not managed by this tool — start one with:
  ```bash
  podman run -d --replace --name grobid -p 8070:8070 docker.io/grobid/grobid:0.9.1-crf
  ```
  The first start takes ~30 s while the models load. `--replace` swaps out an existing `grobid`
  container (e.g. one from an older image). Docker has no `--replace`: run `docker rm -f grobid`
  first, then the same command with `docker` instead of `podman` and without `--replace`.

## Installation

Install the `reference-audit` command as a uv tool, straight from GitHub:

```bash
uv tool install git+https://github.com/constructorfabric/reference-audit.git
uv tool upgrade reference-audit      # later, to update it
```

For development, clone the repository and run `uv sync` instead; see [Development](development.md).

## Settings

Settings, including API keys, are read from these places; a higher one wins for any setting
several define:

1. Command-line options (`--model`, `--backend`, …).
2. Environment variables, which suit CI, containers and scheduled runs.
3. `.env` in the current directory: per-project overrides in dotenv format (git-ignored in this
   repository).
4. `~/.config/reference-audit/config.toml` (`$XDG_CONFIG_HOME/reference-audit/config.toml` when
   that is set): the per-user file, read from any directory. This is the place for an installed
   package. Keep it private: `chmod 600`.

A missing file is skipped. The TOML file's keys are the lower-case `AuditConfig` field names (in
`config.py`). An unknown key there is an error, not a silently ignored typo; the tool exits with
code 2 and names it. An example:

```toml
# Optional — enables LLM adjudication
openai_api_key = "sk-..."
# openai_base_url = "..."          # optional — an OpenAI-compatible endpoint
# Optional — sampling temperature. Unset ⇒ the model default (gpt-6-luna accepts only its default);
# set it to 0 when overriding --model with one that supports it, for more stable verdicts
# llm_temperature = 0.0

# Optional — improve coverage / rate limits for the data sources
s2_api_key = "..."
openalex_api_key = "..."                # OpenAlex API key, sent as its `api_key` query parameter
crossref_mailto = "you@example.org"     # Crossref/OpenAlex polite-pool contact
unpaywall_email = "you@example.org"     # used as that contact when crossref_mailto is unset
openlibrary_email = "you@example.org"   # sent in the User-Agent on Open Library requests (polite identification)
google_books_api_key = "..."            # Google Books per-project quota (the keyless endpoint shares a global daily quota that is routinely exhausted)

# Optional — only used for PDF input; overridable per-run with --grobid
grobid_url = "http://localhost:8070"

# Optional — read Semantic Scholar, OpenAlex and DBLP from a local ClickHouse mirror instead of their
# APIs (see clickhouse-backend.md); overridable per-run with --backend
source_backend = "clickhouse"           # default: "api"
clickhouse_host = "127.0.0.1"
clickhouse_port = 8123
clickhouse_user = "default"
clickhouse_password = "..."
# clickhouse_s2_db = "s2ag"  clickhouse_openalex_db = "openalex"  clickhouse_dblp_db = "kb"  (the defaults)

# Optional — a Chrome/Chromium binary for rendering JavaScript pages, if none is on PATH
# web_render_browser_path = "/usr/bin/chromium"
```

In `.env` and the environment the same settings are upper-cased: `OPENAI_API_KEY=sk-...`,
`SOURCE_BACKEND=clickhouse`, `USE_LLM=false`, and so on. Three have different names there:
`unpaywall_email` is `PAPER_SEARCH_MCP_UNPAYWALL_EMAIL`, `clickhouse_password` is
`CLICKHOUSE_DEFAULT_USER_PASSWORD` (or `CLICKHOUSE_PASSWORD`), and `clickhouse_s2_db` also accepts
`CLICKHOUSE_DB`. Unrelated variables in `.env` or the environment are ignored. Any other
`AuditConfig` field can be set the same way, e.g. `model` replaces the default model and
`use_llm = false` turns the LLM off.

Only `openai_api_key` is needed to run the full pipeline; the data sources used by default
(Crossref, OpenAlex, Semantic Scholar, arXiv, DBLP, Open Library, and the publisher and cited-page
fetches) require no key. Google Books works without a key but on a
shared global daily quota that is frequently exhausted — set `google_books_api_key` for reliable
book coverage at scale. Likewise, set `openalex_api_key` for OpenAlex's keyed daily allowance; an
invalid key makes every OpenAlex query fail (HTTP 401, reported per entry), never "not found". Without `openai_api_key`, or with `--no-llm` (see [Usage](usage.md#options)), the LLM step is skipped and entries that need it are reported as unresolved, with the reason.
