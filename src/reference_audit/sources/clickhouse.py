"""Local ClickHouse backend for Semantic Scholar, OpenAlex and DBLP.

The same three databases the API adapters query, read from a local ClickHouse mirror instead (the S2
Academic Graph dump in `s2ag`, the OpenAlex snapshot in `openalex`, the DBLP dump in `kb`). There are
no rate limits and no third-party outages, so a large batch (a benchmark split, a whole conference)
audits in minutes. Selected per run with `source_backend = "clickhouse"`. The API adapters remain the
default.

**Same source, different transport.** Each adapter keeps its API sibling's `name` (`semantic_scholar`,
`openalex`, `dblp`), and its rows are shaped like the API's JSON and passed through the *same*
normalizer wherever one exists. Routing, field priorities, identity pinning and reports therefore treat
both backends alike. Cached responses are kept apart by `cache_source`, and verdicts by the cache's
`backend` key.

**Title search** uses a full-text `text` index on each table's `title` (lower-cased, split on
non-alphanumerics), with all searchable words required (`hasAllTokens`) and the shortest titles first.
The same contract as DBLP's SPARQL word search, so the exact title ranks ahead of the longer titles
that quote it. Among titles of equal length the most-cited comes first, as an API's relevance ranking
would put it (the 2017 "Attention Is All You Need" ahead of a later repost with the same title). Without the index the search is a scan of hundreds of millions of rows, so `preflight`
refuses to run without it, quoting the statements that build it.

All-words matching is strict: one misspelt or extra word in the cited title hides the real record.
So when no all-words hit is a near-exact title (`title_ratio` below `title_accept`) and the title has
at least `RELAXED_MIN_WORDS` distinct words, the search is repeated with each word left out in turn
(one query, the leave-one-out conditions OR-ed, still served by the index). Those hits rank by edit
distance to the cited title, then citations, and come ahead of the all-words hits. Two wrong words
still find nothing; the APIs' relevance search is more forgiving than that.

**Coverage.** `preflight` also reads how far each snapshot reaches (`coverage_end`): DBLP's latest
dump date, OpenAlex's newest `updated_date`, and the newest publication date in the S2 dump. The
pipeline names it next to a `none` verdict for an entry from that year or later.

**Failures are errors, never "not found":** an unreachable server, a timeout or a query error becomes
`SourceQueryResult.error`, which the pipeline reports and retries next run.

What the local data cannot supply, compared with the APIs:
- OpenAlex has no `locations` table locally, so a Work's version links are its primary location and
  its best open-access location only. The preprint↔published merge then relies on those and on the
  identifier links the other sources supply.
- The DBLP dump table has no landing-page URL (`ee`) and no pages.
- Coverage ends at each snapshot's ingest date. A work newer than the snapshot is not found locally.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import date, datetime
from typing import Any

from reference_audit.config import AuditConfig
from reference_audit.matching.features import title_ratio
from reference_audit.models import (
    BibEntry,
    EntryType,
    Identifiers,
    SourceQueryResult,
    SourceRecord,
)
from reference_audit.sources.base import SourceAdapter
from reference_audit.sources.normalize import (
    dblp_dump_row_to_record,
    openalex_work_to_record,
    s2_paper_to_record,
)
from reference_audit.sources.titlewords import title_search_words

# The full-text title index every searched table must carry (see README, "Local ClickHouse backend").
TITLE_INDEX = "idx_title_text"
TITLE_INDEX_DDL = (
    "ALTER TABLE {table} ADD INDEX IF NOT EXISTS " + TITLE_INDEX + " title "
    "TYPE text(tokenizer = splitByNonAlpha, preprocessor = lower(title));\n"
    "ALTER TABLE {table} MATERIALIZE INDEX " + TITLE_INDEX + ";"
)


# The leave-one-word-out retry needs at least this many distinct title words: with fewer, the words
# left would match far too many titles to rank the right one into the first page.
RELAXED_MIN_WORDS = 4


def _all_words_condition(words: list[str]) -> tuple[str, dict]:
    return "hasAllTokens(title, {w:Array(String)})", {"w": words}


def _leave_one_out_condition(words: list[str]) -> tuple[str, dict]:
    """Every distinct word but one must match, for each word in turn."""
    distinct = list(dict.fromkeys(words))
    subsets = [distinct[:i] + distinct[i + 1:] for i in range(len(distinct))]
    sql = " OR ".join(f"hasAllTokens(title, {{w{i}:Array(String)}})" for i in range(len(subsets)))
    return f"({sql})", {f"w{i}": sub for i, sub in enumerate(subsets)}


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


class ClickHouseUnavailableError(RuntimeError):
    """The local ClickHouse backend cannot serve this run: unreachable, or missing a title index."""


class ClickHouseQueryError(Exception):
    """One query failed (timeout, server error, transport). Mapped to a source *error*."""


class ClickHouseConnection:
    """One client for the run, shared by the three adapters, with bounded concurrency.

    `clickhouse_connect`'s client is synchronous, so queries run in worker threads. A semaphore keeps
    at most `concurrency` of them in flight, because the pipeline starts every entry at once. Session
    ids are off: queries from one client share a session otherwise, and a session refuses concurrent
    queries.
    """

    def __init__(self, config: AuditConfig):
        self.config = config
        self._client: Any = None
        self._client_lock = threading.Lock()
        self._semaphore = asyncio.Semaphore(max(1, config.clickhouse_concurrency))
        self._ready: asyncio.Future | None = None
        self._closed = False

    def _get_client(self):
        with self._client_lock:
            if self._client is None:
                import clickhouse_connect

                self._client = clickhouse_connect.get_client(
                    host=self.config.clickhouse_host,
                    port=self.config.clickhouse_port,
                    username=self.config.clickhouse_user,
                    password=self.config.clickhouse_password,
                    autogenerate_session_id=False,
                    connect_timeout=10,
                    send_receive_timeout=int(self.config.clickhouse_query_timeout) + 30,
                )
            return self._client

    def _query_sync(self, sql: str, parameters: dict | None) -> list[dict]:
        settings = {
            "max_execution_time": int(self.config.clickhouse_query_timeout),
            # make max_execution_time a wall-clock limit rather than a projected-speed check
            "timeout_before_checking_execution_speed": 0,
            "readonly": 2,
        }
        result = self._get_client().query(sql, parameters=parameters, settings=settings)
        return list(result.named_results())

    async def query(self, sql: str, parameters: dict | None = None) -> list[dict]:
        """Rows as dicts. Any failure raises ClickHouseQueryError (caller maps it to a source error)."""
        async with self._semaphore:
            try:
                return await asyncio.to_thread(self._query_sync, sql, parameters)
            except Exception as exc:  # transport, timeout, server-side error — all an *error*
                raise ClickHouseQueryError(f"clickhouse: {type(exc).__name__}: {exc}") from exc

    async def ensure_ready(self, tables: list[str]) -> None:
        """Check reachability and the title indexes once per run; every adapter awaits the same check."""
        if self._ready is None:
            self._ready = asyncio.ensure_future(self._check(tables))
        await self._ready

    async def _check(self, tables: list[str]) -> None:
        cfg = self.config
        where = f"{cfg.clickhouse_host}:{cfg.clickhouse_port}"
        try:
            await self.query("SELECT 1")
        except ClickHouseQueryError as exc:
            raise ClickHouseUnavailableError(
                f"source_backend=clickhouse, but the ClickHouse server at {where} cannot be queried "
                f"({exc}). Start it, fix CLICKHOUSE_* in .env, or run with the API backend "
                "(SOURCE_BACKEND=api / --backend api)."
            ) from exc
        problems: list[str] = []
        for table in tables:
            database, name = table.split(".", 1)
            rows = await self.query(
                "SELECT type FROM system.data_skipping_indices "
                "WHERE database = {db:String} AND table = {t:String} AND name = {i:String}",
                {"db": database, "t": name, "i": TITLE_INDEX},
            )
            if not rows or rows[0]["type"] != "text":
                problems.append(f"{table} has no full-text index {TITLE_INDEX}. Build it with:\n"
                                + TITLE_INDEX_DDL.format(table=table))
                continue
            pending = await self.query(
                "SELECT count() AS n FROM system.mutations WHERE database = {db:String} "
                "AND table = {t:String} AND NOT is_done AND position(command, {i:String}) > 0",
                {"db": database, "t": name, "i": TITLE_INDEX},
            )
            if pending and pending[0]["n"]:
                problems.append(
                    f"{table}: {TITLE_INDEX} is still being built (a MATERIALIZE INDEX mutation is "
                    "running); parts without it would be scanned in full. Wait for it to finish "
                    "(system.mutations)."
                )
        if problems:
            raise ClickHouseUnavailableError(
                f"source_backend=clickhouse, but the server at {where} cannot serve title search:\n"
                + "\n".join(problems)
            )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        client, self._client = self._client, None
        if client is not None:
            await asyncio.to_thread(client.close)


def _error(name: str, kind: str, exc: Exception) -> SourceQueryResult:
    return SourceQueryResult(source=name, query_kind=kind, error=str(exc))


def _no_words(name: str, title: str) -> SourceQueryResult:
    # Nothing the word index can match. An empty result would read as "the database has no such
    # paper" when it was never actually asked.
    return SourceQueryResult(
        source=name, query_kind="metadata",
        error=f"title has no searchable word for the title index: {title!r}",
    )


class _ClickHouseAdapter(SourceAdapter):
    """Shared plumbing: the connection, the per-run preflight, title search, and closing.

    A subclass supplies `_title_rows` (one page of title hits, each with `id` and title `t`),
    `_records` (those hits as `SourceRecord`s, in order), the two orderings, and `coverage_sql`.
    """

    backend = "clickhouse"
    rate_per_sec = 1000.0  # local server; concurrency is bounded by the connection's semaphore
    strict_order = ""      # ORDER BY of the all-words search
    relaxed_order = ""     # ORDER BY of the leave-one-out search; may use {cited:String}

    def __init__(self, connection: ClickHouseConnection, **kw):
        super().__init__(**kw)
        self.ch = connection

    def search_tables(self) -> list[str]:
        raise NotImplementedError

    def coverage_sql(self) -> str:
        """One row, one column `d`: the newest date this snapshot covers."""
        raise NotImplementedError

    async def preflight(self) -> None:
        await self.ch.ensure_ready(self.search_tables())
        try:
            rows = await self.ch.query(self.coverage_sql())
        except ClickHouseQueryError as exc:
            raise ClickHouseUnavailableError(
                f"source_backend=clickhouse: cannot read how far the local {self.name} snapshot "
                f"reaches ({exc})"
            ) from exc
        self.coverage_end = _as_date(rows[0]["d"]) if rows else None
        if self.coverage_end is None or self.coverage_end.year < 2000:  # 1970: an unset value
            raise ClickHouseUnavailableError(
                f"source_backend=clickhouse: the local {self.name} snapshot reports no usable "
                f"coverage date ({rows[0]['d'] if rows else 'no rows'}); is the table empty?"
            )

    async def _title_rows(self, condition: str, params: dict, order: str, limit: int) -> list[dict]:
        raise NotImplementedError

    async def _records(self, rows: list[dict]) -> list[SourceRecord]:
        raise NotImplementedError

    async def search_by_metadata(self, entry: BibEntry, limit: int = 10) -> SourceQueryResult:
        if not entry.title:
            return SourceQueryResult(source=self.name, query_kind="metadata", records=[])
        words = title_search_words(entry.title)
        if not words:
            return _no_words(self.name, entry.title)
        try:
            rows = await self._title_rows(*_all_words_condition(words), self.strict_order, limit)
            near_exact = any(
                title_ratio(entry.title, r["t"]) >= self.ch.config.title_accept for r in rows
            )
            if not near_exact and len(set(words)) >= RELAXED_MIN_WORDS:
                condition, params = _leave_one_out_condition(words)
                params["cited"] = entry.title.lower()
                relaxed = await self._title_rows(condition, params, self.relaxed_order, limit)
                seen = {r["id"] for r in relaxed}
                rows = (relaxed + [r for r in rows if r["id"] not in seen])[:limit]
            records = await self._records(rows)
        except ClickHouseQueryError as exc:
            return _error(self.name, "metadata", exc)
        return SourceQueryResult(source=self.name, query_kind="metadata", records=records)

    async def aclose(self) -> None:
        await super().aclose()
        await self.ch.aclose()


# ── Semantic Scholar (S2 Academic Graph dump) ─────────────────────────────────────────────────────
_S2_ID_TYPES = (("doi", "doi"), ("arxiv_id", "arxiv"), ("pmid", "pubmed"))


class ClickHouseSemanticScholarAdapter(_ClickHouseAdapter):
    name = "semantic_scholar"
    handles = {EntryType.ARTICLE, EntryType.INPROCEEDINGS, EntryType.MISC}

    @property
    def db(self) -> str:
        return self.ch.config.clickhouse_s2_db

    strict_order = "length(t), c DESC, id"
    relaxed_order = "editDistance(lower(t), {cited:String}), c DESC, id"

    def search_tables(self) -> list[str]:
        return [f"{self.db}.papers"]

    def coverage_sql(self) -> str:
        # The S2 dump has no snapshot date; its newest (non-future) publication date stands in.
        return (
            f"SELECT max(publicationdate) AS d FROM {self.db}.papers "
            "WHERE publicationdate <= toString(today())"
        )

    async def lookup_by_id(self, ids: Identifiers) -> SourceQueryResult:
        # The API looks up DOI, else arXiv, else PMID; so does this (the same precedence).
        key = next(((t, getattr(ids, f)) for f, t in _S2_ID_TYPES if getattr(ids, f)), None)
        if key is None:
            return SourceQueryResult(source=self.name, query_kind="id", records=[])
        id_type, value = key
        try:
            rows = await self.ch.query(
                f"SELECT DISTINCT corpusid FROM {self.db}.paper_external_ids "
                "WHERE id_type = {t:String} AND id_val = {v:String} LIMIT 5",
                {"t": id_type, "v": value.lower()},
            )
            records = await self._papers([r["corpusid"] for r in rows])
        except ClickHouseQueryError as exc:
            return _error(self.name, "id", exc)
        return SourceQueryResult(source=self.name, query_kind="id", records=records)

    async def _title_rows(self, condition: str, params: dict, order: str, limit: int) -> list[dict]:
        return await self.ch.query(
            f"SELECT corpusid AS id, any(title) AS t, max(ifNull(citationcount, 0)) AS c "
            f"FROM {self.db}.papers WHERE {condition} "
            f"GROUP BY id ORDER BY {order} LIMIT {{n:UInt32}}",
            {**params, "n": limit},
        )

    async def _records(self, rows: list[dict]) -> list[SourceRecord]:
        return await self._papers([r["id"] for r in rows])

    async def _papers(self, corpusids: list[int]) -> list[SourceRecord]:
        """Full records for `corpusids`, in the given order (newest ingest of each)."""
        if not corpusids:
            return []
        papers = await self.ch.query(
            "SELECT corpusid, title, venue, year, externalids, citationcount, "
            "arrayMap(a -> ifNull(a.name, ''), authors) AS author_names "
            f"FROM {self.db}.papers WHERE corpusid IN {{ids:Array(UInt64)}} "
            "ORDER BY corpusid, ingest_seq DESC LIMIT 1 BY corpusid",
            {"ids": corpusids},
        )
        abstracts = await self.ch.query(
            f"SELECT corpusid, abstract FROM {self.db}.abstracts "
            "WHERE corpusid IN {ids:Array(UInt64)} LIMIT 1 BY corpusid",
            {"ids": corpusids},
        )
        abstract_of = {r["corpusid"]: r["abstract"] for r in abstracts}
        by_id = {r["corpusid"]: r for r in papers}
        return [
            s2_paper_to_record(s2_row_to_api_paper(by_id[c], abstract_of.get(c, "")))
            for c in corpusids if c in by_id
        ]


def s2_row_to_api_paper(row: dict, abstract: str) -> dict:
    """An `s2ag.papers` row in the S2 Graph API's paper shape, for `s2_paper_to_record`."""
    try:
        external = json.loads(row.get("externalids") or "{}")
    except ValueError:
        external = {}
    return {
        "paperId": str(row["corpusid"]),
        "title": row.get("title") or "",
        "year": row.get("year"),
        "venue": row.get("venue") or "",
        "authors": [{"name": n} for n in row.get("author_names") or [] if n],
        "externalIds": {k: v for k, v in external.items() if v is not None},
        "citationCount": row.get("citationcount") or 0,
        "abstract": abstract or "",
    }


# ── OpenAlex (snapshot) ───────────────────────────────────────────────────────────────────────────
_OPENALEX_SLIM_COLUMNS = (
    "id, doi, ids, title, publication_year, type, cited_by_count, "
    "arrayMap(a -> ifNull(a.author.display_name, ''), authorships) AS author_names, "
    "primary_location.landing_page_url AS landing_page_url, primary_location.pdf_url AS pdf_url, "
    "primary_location.source.display_name AS source_name, primary_location.source.type AS source_type"
)


class ClickHouseOpenAlexAdapter(_ClickHouseAdapter):
    name = "openalex"
    handles = {
        EntryType.ARTICLE,
        EntryType.INPROCEEDINGS,
        EntryType.MISC,
        EntryType.BOOK,
        EntryType.INCOLLECTION,
    }

    @property
    def db(self) -> str:
        return self.ch.config.clickhouse_openalex_db

    strict_order = "length(t), c DESC, id"
    relaxed_order = "editDistance(lower(t), {cited:String}), c DESC, id"

    def search_tables(self) -> list[str]:
        return [f"{self.db}.works_slim"]

    def coverage_sql(self) -> str:
        return f"SELECT max(updated_date) AS d FROM {self.db}.works"

    async def lookup_by_id(self, ids: Identifiers) -> SourceQueryResult:
        # The API's precedence: DOI, else arXiv (via its DataCite DOI), else a cited Work id.
        if ids.doi:
            column, value = "doi", f"https://doi.org/{ids.doi.lower()}"
        elif ids.arxiv_id:
            column, value = "doi", f"https://doi.org/10.48550/arxiv.{ids.arxiv_id.lower()}"
        elif ids.openalex:
            column, value = "id", f"https://openalex.org/{ids.openalex}"
        else:
            return SourceQueryResult(source=self.name, query_kind="id", records=[])
        try:
            rows = await self.ch.query(
                f"SELECT id FROM {self.db}.works_slim WHERE {column} = {{v:String}} LIMIT 5",
                {"v": value},
            )
            records = await self._works(list(dict.fromkeys(r["id"] for r in rows)))
        except ClickHouseQueryError as exc:
            return _error(self.name, "id", exc)
        return SourceQueryResult(source=self.name, query_kind="id", records=records)

    async def _title_rows(self, condition: str, params: dict, order: str, limit: int) -> list[dict]:
        return await self.ch.query(
            f"SELECT id, any(title) AS t, max(ifNull(cited_by_count, 0)) AS c "
            f"FROM {self.db}.works_slim WHERE {condition} "
            f"GROUP BY id ORDER BY {order} LIMIT {{n:UInt32}}",
            {**params, "n": limit},
        )

    async def _records(self, rows: list[dict]) -> list[SourceRecord]:
        return await self._works([r["id"] for r in rows])

    async def _works(self, work_ids: list[str]) -> list[SourceRecord]:
        """Full records for `work_ids`, in the given order: the slim table for the work, the full
        `works` table for pages (`biblio`) and the abstract."""
        if not work_ids:
            return []
        slim = await self.ch.query(
            f"SELECT {_OPENALEX_SLIM_COLUMNS} FROM {self.db}.works_slim "
            "WHERE id IN {ids:Array(String)} ORDER BY id, updated_date DESC LIMIT 1 BY id",
            {"ids": work_ids},
        )
        full = await self.ch.query(
            "SELECT id, biblio.volume AS volume, biblio.issue AS issue, "
            "biblio.first_page AS first_page, biblio.last_page AS last_page, "
            "best_oa_location.landing_page_url AS oa_landing_page_url, "
            "best_oa_location.pdf_url AS oa_pdf_url, "
            f"abstract_inverted_index FROM {self.db}.works "
            "WHERE id IN {ids:Array(String)} ORDER BY id, version DESC LIMIT 1 BY id",
            {"ids": work_ids},
        )
        extra = {r["id"]: r for r in full}
        by_id = {r["id"]: r for r in slim}
        return [
            openalex_work_to_record(openalex_row_to_api_work(by_id[w], extra.get(w)))
            for w in work_ids if w in by_id
        ]


def _best_oa_location(full: dict, primary: dict) -> list[dict]:
    """The `works` row's best open-access location, unless it is missing or is the primary one."""
    landing, pdf = full.get("oa_landing_page_url"), full.get("oa_pdf_url")
    if not (landing or pdf):
        return []
    if (landing, pdf) == (primary.get("landing_page_url"), primary.get("pdf_url")):
        return []
    return [{"landing_page_url": landing, "pdf_url": pdf}]


def openalex_row_to_api_work(slim: dict, full: dict | None) -> dict:
    """A `works_slim` row (+ its `works` biblio/abstract) in the OpenAlex API's Work shape."""
    full = full or {}
    try:
        abstract_index = json.loads(full.get("abstract_inverted_index") or "null")
    except ValueError:
        abstract_index = None
    primary = {
        "landing_page_url": slim.get("landing_page_url"),
        "pdf_url": slim.get("pdf_url"),
        "source": {"display_name": slim.get("source_name"), "type": slim.get("source_type")},
    }
    return {
        "id": slim["id"],
        "doi": slim.get("doi"),
        "ids": dict(slim.get("ids") or {}),
        "title": slim.get("title") or "",
        "publication_year": slim.get("publication_year"),
        "type": slim.get("type"),
        "authorships": [{"author": {"display_name": n}} for n in slim.get("author_names") or [] if n],
        "primary_location": primary,
        # No `locations` table locally: the primary and the best open-access location are all
        # that is known. The latter is often the arXiv copy of a published paper.
        "locations": [primary, *_best_oa_location(full, primary)],
        "biblio": {k: full.get(k) for k in ("volume", "issue", "first_page", "last_page")},
        "cited_by_count": slim.get("cited_by_count") or 0,
        "abstract_inverted_index": abstract_index if isinstance(abstract_index, dict) else None,
    }


# ── DBLP (dump) ───────────────────────────────────────────────────────────────────────────────────
class ClickHouseDblpAdapter(_ClickHouseAdapter):
    name = "dblp"
    handles = {EntryType.ARTICLE, EntryType.INPROCEEDINGS, EntryType.MISC}

    @property
    def db(self) -> str:
        return self.ch.config.clickhouse_dblp_db

    strict_order = "length(title), key, ingested_at DESC"
    relaxed_order = "editDistance(lower(title), {cited:String}), key, ingested_at DESC"

    def search_tables(self) -> list[str]:
        return [f"{self.db}.dblp_publication"]

    def coverage_sql(self) -> str:
        return f"SELECT max(dump_date) AS d FROM {self.db}.dblp_publication"

    async def _title_rows(self, condition: str, params: dict, order: str, limit: int) -> list[dict]:
        table = f"{self.db}.dblp_publication"
        # The table keeps one partition per dump; only the latest dump is the current DBLP.
        rows = await self.ch.query(
            "SELECT key, kind, publtype, title, year, venue, authors, dois "
            f"FROM {table} WHERE dump_date = (SELECT max(dump_date) FROM {table}) "
            f"AND {condition} ORDER BY {order} LIMIT 1 BY key LIMIT {{n:UInt32}}",
            {**params, "n": limit},
        )
        return [{**r, "id": r["key"], "t": r["title"]} for r in rows]

    async def _records(self, rows: list[dict]) -> list[SourceRecord]:
        return [
            dblp_dump_row_to_record({k: v for k, v in r.items() if k not in ("id", "t")})
            for r in rows
        ]


def build_clickhouse_adapters(config: AuditConfig) -> list[SourceAdapter]:
    """The three local adapters, sharing one connection."""
    connection = ClickHouseConnection(config)
    return [
        ClickHouseOpenAlexAdapter(connection),
        ClickHouseSemanticScholarAdapter(connection),
        ClickHouseDblpAdapter(connection),
    ]
