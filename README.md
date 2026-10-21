# reference-audit

[![PyPI](https://img.shields.io/pypi/v/reference-audit)](https://pypi.org/project/reference-audit/)
[![Tests](https://github.com/constructorfabric/reference-audit/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/constructorfabric/reference-audit/actions/workflows/tests.yml)
[![License](https://img.shields.io/github/license/constructorfabric/reference-audit)](LICENSE)

Audit the references in a paper. Given a `.bib` bibliography and the `.tex` that cites it — or just
the paper's **PDF** or **Word `.docx` with a structured bibliography** — `reference-audit` figures out **which real-world document each reference
actually points to** and flags ones that don't match anything real (hallucinated references).

For every entry it returns one of four verdicts:

- **exactly one match** — the reference resolves to a single real document (with its DOI/ISBN/URL,
  and any preprint↔published versions merged).
- **no match** — no real document corresponds to the entry (a likely hallucination).
- **multiple matches** — several accepted candidate groups remain after same-work clustering.
- **contradiction** — a hard paper identifier resolves to a real paper, but supplied citation fields
  substantively disagree with that paper. The report preserves the ID target and conflict evidence.

It also backfills missing DOIs/ISBNs, normalizes malformed identifiers, and reports dangling or
uncited citations.

> Designed for three audiences: authors polishing their bibliography, automated screening of
> submissions for hallucinated references, and AI agents writing papers. The full product
> specification lives in [`architecture/SPEC.md`](architecture/SPEC.md).

## Quick Start

You need [`uv`](https://docs.astral.sh/uv/getting-started/installation/). Install the tool from
PyPI (uv fetches Python 3.14 if your system lacks it):

```bash
uv tool install reference-audit
```

Then audit a bibliography:

```bash
reference-audit audit references.bib
```

Pass the manuscript before the `.bib` (`reference-audit audit paper.tex references.bib`) to also
find entries that are never cited and `\cite` keys with no entry. With only a PDF, see
[Auditing a PDF](docs/pdf-input.md).

For Word documents (including the AI4X template), run `reference-audit audit paper.docx`.
**WARNING: Word audits check only Word's native structured bibliography. Free-form / manually
typed references are not checked and may be missed, even when structured sources are also present.**
Every Word run prints this warning prominently. See [Auditing Word documents](docs/word-input.md)
for supported fields and limits; `--no-network` works without Word or GROBID.

No accounts or keys are needed for a first run. The report opens with the two lists that matter
most:

- **`CAPITAL OFFENCES`**: entries that match no real document, which makes them likely hallucinations.
- **`UNABLE TO VERIFY`**: entries the tool could not check, each with the reason (a source that
  failed, an ambiguity it could not settle, a dead link). These are not hallucinations. They are
  references a person still has to check.

Below them, `ISSUES` and `FORMATTING NITS & ADVISORIES` list wrong or missing fields, DOIs, and
better versions to cite. [Reading the output](docs/usage.md#reading-the-output) explains every
line. Add `--format json` for a machine-readable report.

Results are cached in `.reference_audit/cache.db` beside the `.bib`, `.pdf`, or `.docx`, so a re-run after you fix some
entries only re-checks what changed and what failed last time.

Settings and API keys go in `~/.config/reference-audit/config.toml`. Keep the file private
(`chmod 600`). The two sections below describe the settings worth adding.
[Configuration](docs/configuration.md) lists every setting and the other places they can be set.

## Adding API keys for the data sources

Without keys, the scholarly databases serve the tool from their shared public allowances. On a large
bibliography, or when you run audits often, some lookups then fail with rate-limit or quota errors.
Those entries end up under `UNABLE TO VERIFY` naming the source and its error, and
retries with backoff slow the run down. Registering with the sources gives you your own allowance:

```toml
crossref_mailto = "you@example.org"      # Crossref and OpenAlex "polite pool" contact
openlibrary_email = "you@example.org"    # identifies you to Open Library
s2_api_key = "..."                       # Semantic Scholar: https://www.semanticscholar.org/product/api
openalex_api_key = "..."                 # OpenAlex: https://docs.openalex.org
google_books_api_key = "..."             # Google Books: https://developers.google.com/books/docs/v1/using#APIKey
```

Add only the ones you need. Failures from earlier runs are never cached, so the next run retries them.
An invalid key makes every query to that source fail, and each affected entry says so; it is never
read as "not found". For very large batches, Semantic Scholar, OpenAlex and DBLP can also be read
from a [local ClickHouse mirror](docs/clickhouse-backend.md), which has no rate limits.

## Adding an LLM API key

The tool decides most entries by deterministic rules. Some it cannot settle that way: a candidate
that is close to the entry but not an exact match, two records that may or may not be the same
work, a field difference no rule covers. With an LLM, each of these is judged one candidate at a
time. Without one, those entries are reported under `UNABLE TO VERIFY` with the reason `… need LLM
adjudication, but the LLM is not configured`. Nothing is guessed either way.

To turn the LLM on, add an OpenAI key, or the key and URL of any OpenAI-compatible endpoint:

```toml
openai_api_key = "sk-..."
# openai_base_url = "https://..."        # an OpenAI-compatible endpoint
# model = "..."                          # default: gpt-6-luna; or pass --model per run
```

The LLM also enables [`--check-citations`](#checking-what-citations-claim). LLM answers are cached
like everything else. Use `--no-llm` for a deterministic run even when a key is configured.

## Checking what citations claim

A reference can be real and still be cited for something its source never says. With an LLM
configured, `--check-citations` checks each citing sentence against the cited work's abstract:

```bash
reference-audit audit paper.tex references.bib --check-citations
```

Only references that resolve to exactly one work are checked. Each citation gets one of four labels:

- **`supported`**: the abstract backs the claim.
- **`contradicted`**: the abstract says the opposite. This is reported as an issue
  (*citation may misrepresent the source*) and quotes the abstract.
- **`not_in_abstract`**: the abstract does not mention the claim. An abstract is only a summary, so
  this is an advisory note, not a finding of misuse.
- **`unverifiable`**: no abstract could be found, or the LLM was unavailable or failed. The report
  says so and nothing is guessed.

The check reads abstracts only, never full text, and it never changes a reference's verdict. It
needs citing sentences, which come from the `.tex`, from a Word document's citation fields, or from
a PDF read with GROBID. A lone `.bib` or a PDF read by the language model has none, and the report
says that alignment was not checked. `--format json` carries every finding under
`alignment_findings`, with its evidence quote and confidence. See
[Citation alignment](docs/usage.md#citation-alignment---check-citations) for details.

## Documentation

- [How it works](docs/how-it-works.md): the identification funnel, the sources, books, web pages,
  and caching.
- [Usage](docs/usage.md): command-line options, reading the report, and citation alignment
  (`--check-citations`).
- [Installation and configuration](docs/configuration.md): requirements, installation, and every
  setting.
- [Auditing a PDF](docs/pdf-input.md): reading the references with a language model or GROBID, and
  PDFs that embed their references.
- [Auditing Word documents](docs/word-input.md): native structured bibliography and citation fields,
  with a prominent warning that free-form references are not checked.
- [Local ClickHouse backend](docs/clickhouse-backend.md): reading Semantic Scholar, OpenAlex and
  DBLP from a local mirror.
- [Development](docs/development.md): tests, the PDF extraction oracle, benchmarking, and the project
  layout.
- [HALLMARK benchmark](benchmarks/README.md): verdict accuracy on a citation-hallucination benchmark.
- [Constructor Fabric](docs/constructor-fabric.md): how the project is specified, traced, and
  validated with `cfs`.
- [Product specification](architecture/SPEC.md) and the governed design artifacts in
  [`architecture/`](architecture/).
