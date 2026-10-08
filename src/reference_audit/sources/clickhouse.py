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

**Failures are errors, never "not found":** an unreachable server, a timeout or a query error becomes
`SourceQueryResult.error`, which the pipeline reports and retries next run.

What the local data cannot supply, compared with the APIs:
- OpenAlex has no `locations` table locally, so a Work's version links are its primary location only.
  The preprint↔published merge then relies on the identifier links the other sources supply.
- The DBLP dump table has no landing-page URL (`ee`) and no pages.
- Coverage ends at each snapshot's ingest date. A work newer than the snapshot is not found locally.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from reference_audit.config import AuditConfig
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
    """Shared plumbing: the connection, the per-run preflight, and closing."""

    backend = "clickhouse"
    rate_per_sec = 1000.0  # local server; concurrency is bounded by the connection's semaphore

    def __init__(self, connection: ClickHouseConnection, **kw):
        super().__init__(**kw)
        self.ch = connection

    def search_tables(self) -> list[str]:
        raise NotImplementedError

    async def preflight(self) -> None:
        await self.ch.ensure_ready(self.search_tables())

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

    def search_tables(self) -> list[str]:
        return [f"{self.db}.papers"]

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

    async def search_by_metadata(self, entry: BibEntry, limit: int = 10) -> SourceQueryResult:
        if not entry.title:
            return SourceQueryResult(source=self.name, query_kind="metadata", records=[])
        words = title_search_words(entry.title)
        if not words:
            return _no_words(self.name, entry.title)
        try:
            rows = await self.ch.query(
                f"SELECT corpusid, any(title) AS t, max(ifNull(citationcount, 0)) AS c "
                f"FROM {self.db}.papers WHERE hasAllTokens(title, {{w:Array(String)}}) "
                "GROUP BY corpusid ORDER BY length(t), c DESC, corpusid LIMIT {n:UInt32}",
                {"w": words, "n": limit},
            )
            records = await self._papers([r["corpusid"] for r in rows])
        except ClickHouseQueryError as exc:
            return _error(self.name, "metadata", exc)
        return SourceQueryResult(source=self.name, query_kind="metadata", records=records)

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

    def search_tables(self) -> list[str]:
        return [f"{self.db}.works_slim"]

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

    async def search_by_metadata(self, entry: BibEntry, limit: int = 10) -> SourceQueryResult:
        if not entry.title:
            return SourceQueryResult(source=self.name, query_kind="metadata", records=[])
        words = title_search_words(entry.title)
        if not words:
            return _no_words(self.name, entry.title)
        try:
            rows = await self.ch.query(
                f"SELECT id, any(title) AS t, max(ifNull(cited_by_count, 0)) AS c "
                f"FROM {self.db}.works_slim WHERE hasAllTokens(title, {{w:Array(String)}}) "
                "GROUP BY id ORDER BY length(t), c DESC, id LIMIT {n:UInt32}",
                {"w": words, "n": limit},
            )
            records = await self._works([r["id"] for r in rows])
        except ClickHouseQueryError as exc:
            return _error(self.name, "metadata", exc)
        return SourceQueryResult(source=self.name, query_kind="metadata", records=records)

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
        # No `locations` table locally: the primary location is the only one known.
        "locations": [primary],
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

    def search_tables(self) -> list[str]:
        return [f"{self.db}.dblp_publication"]

    async def search_by_metadata(self, entry: BibEntry, limit: int = 10) -> SourceQueryResult:
        if not entry.title:
            return SourceQueryResult(source=self.name, query_kind="metadata", records=[])
        words = title_search_words(entry.title)
        if not words:
            return _no_words(self.name, entry.title)
        table = f"{self.db}.dblp_publication"
        try:
            # The table keeps one partition per dump; only the latest dump is the current DBLP.
            rows = await self.ch.query(
                "SELECT key, kind, publtype, title, year, venue, authors, dois "
                f"FROM {table} WHERE dump_date = (SELECT max(dump_date) FROM {table}) "
                "AND hasAllTokens(title, {w:Array(String)}) "
                "ORDER BY length(title), key, ingested_at DESC LIMIT 1 BY key LIMIT {n:UInt32}",
                {"w": words, "n": limit},
            )
        except ClickHouseQueryError as exc:
            return _error(self.name, "metadata", exc)
        records = [dblp_dump_row_to_record(r) for r in rows]
        return SourceQueryResult(source=self.name, query_kind="metadata", records=records)


def build_clickhouse_adapters(config: AuditConfig) -> list[SourceAdapter]:
    """The three local adapters, sharing one connection."""
    connection = ClickHouseConnection(config)
    return [
        ClickHouseOpenAlexAdapter(connection),
        ClickHouseSemanticScholarAdapter(connection),
        ClickHouseDblpAdapter(connection),
    ]
