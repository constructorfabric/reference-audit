"""SQLite connection + schema (adapted from sciwrite-lint workspace_db `_core`).

Three cache layers (README: avoid re-running DB/LLM calls):
- source_query_cache : raw adapter responses, keyed by (entry_hash, source, query_kind).
  Only successful (ok=1) results are stored; errors are never cached (so they retry), preserving
  the error≠not-found invariant.
- llm_decision_cache : LLM verdicts keyed by (prompt_hash, kind, model) — model in key ⇒ a model
  switch re-runs (added in M4).
- entry_verdict_cache: whole-entry fast path, keyed by (entry_hash, backend) and gated by
  (pipeline_version, model). `backend` is the source backend that produced the verdict (`api` or
  `clickhouse`): the two read different snapshots of the same databases, so neither may serve the
  other's verdict.
- doi_resolution_cache: doi.org's verdict on a DOI (does the handle resolve?) — a world-fact, so
  keyed by the bare DOI and independent of pipeline_version/model. Only definitive True/False are
  stored; an outage is never cached (mirrors the never-cache-errors invariant).
- db_quirks          : mirror of docs/db_quirks.md (README principle 2).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_query_cache (
    entry_hash TEXT NOT NULL,
    source     TEXT NOT NULL,
    query_kind TEXT NOT NULL,
    result_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    ok         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (entry_hash, source, query_kind)
);
CREATE TABLE IF NOT EXISTS llm_decision_cache (
    prompt_hash TEXT NOT NULL,
    kind        TEXT NOT NULL,
    model       TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (prompt_hash, kind, model)
);
CREATE TABLE IF NOT EXISTS entry_verdict_cache (
    entry_hash       TEXT NOT NULL,
    backend          TEXT NOT NULL DEFAULT 'api',
    verdict_json     TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    model            TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (entry_hash, backend)
);
CREATE TABLE IF NOT EXISTS doi_resolution_cache (
    doi        TEXT PRIMARY KEY,
    resolves   INTEGER NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS db_quirks (
    source        TEXT NOT NULL,
    quirk         TEXT NOT NULL,
    example_entry TEXT,
    noted_at      TEXT NOT NULL
);
"""

_CACHE_TABLES = (
    "source_query_cache",
    "llm_decision_cache",
    "entry_verdict_cache",
    "doi_resolution_cache",
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a cache written by an older version up to the current schema, in place.

    `entry_verdict_cache` gained a `backend` column (part of the key). Every verdict an older cache
    holds was produced through the public APIs, so it is kept and labelled `api`.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(entry_verdict_cache)")}
    if cols and "backend" not in cols:
        conn.executescript(
            """
            BEGIN;
            ALTER TABLE entry_verdict_cache RENAME TO entry_verdict_cache_old;
            CREATE TABLE entry_verdict_cache (
                entry_hash       TEXT NOT NULL,
                backend          TEXT NOT NULL DEFAULT 'api',
                verdict_json     TEXT NOT NULL,
                pipeline_version TEXT NOT NULL,
                model            TEXT NOT NULL,
                created_at       TEXT NOT NULL,
                PRIMARY KEY (entry_hash, backend)
            );
            INSERT INTO entry_verdict_cache
                (entry_hash, backend, verdict_json, pipeline_version, model, created_at)
                SELECT entry_hash, 'api', verdict_json, pipeline_version, model, created_at
                FROM entry_verdict_cache_old;
            DROP TABLE entry_verdict_cache_old;
            COMMIT;
            """
        )


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating dirs + schema) a WAL-mode connection."""
    p = Path(path)
    if p.parent and str(p.parent) not in ("", "."):
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _migrate(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def clear(conn: sqlite3.Connection) -> None:
    for table in _CACHE_TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
