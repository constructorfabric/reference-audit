"""Typed CRUD over the cache DB. The pipeline reads here before any network/LLM call."""

from __future__ import annotations

import datetime
import hashlib
from pathlib import Path

from reference_audit.cache import db as _db
from reference_audit.models import SourceQueryResult, Verdict


def _now() -> str:
    # Fixed width (microseconds always present), so stamps order correctly as text.
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="microseconds")


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AuditCache:
    """Backed by SQLite. `model`/`pipeline_version` gate the whole-entry verdict fast path, and
    `backend` (the source backend, `api` or `clickhouse`) keys it.

    `fresh=True` (`--fresh`) ignores every row stored before this cache was opened: reads only see
    what was written since, and writes overwrite as usual. Nothing is deleted, so other
    bibliographies sharing the DB keep their cached results."""

    def __init__(
        self,
        path: str | Path,
        *,
        pipeline_version: str = "0.1",
        model: str = "",
        backend: str = "api",
        fresh: bool = False,
    ):
        self.conn = _db.connect(path)
        self.pipeline_version = pipeline_version
        self.model = model
        self.backend = backend
        # Rows stamped before this are invisible to reads (ISO-8601 UTC stamps compare as text).
        self.since = _now() if fresh else ""

    # --- source query cache (only successful results are stored) ---
    def get_source_query(
        self, entry_hash: str, source: str, query_kind: str
    ) -> SourceQueryResult | None:
        row = self.conn.execute(
            "SELECT result_json FROM source_query_cache "
            "WHERE entry_hash=? AND source=? AND query_kind=? AND ok=1 AND fetched_at>=?",
            (entry_hash, source, query_kind, self.since),
        ).fetchone()
        return SourceQueryResult.model_validate_json(row["result_json"]) if row else None

    def put_source_query(
        self, entry_hash: str, result: SourceQueryResult, *, source: str | None = None
    ) -> None:
        """Store a successful result under `source` (default `result.source`). An adapter passes its
        `cache_source`, so a local-mirror response never answers for the public API or vice versa."""
        if result.error is not None:
            return  # never cache errors — they must retry (error ≠ not-found)
        self.conn.execute(
            "INSERT OR REPLACE INTO source_query_cache "
            "(entry_hash, source, query_kind, result_json, fetched_at, ok) VALUES (?,?,?,?,?,1)",
            (entry_hash, source or result.source, result.query_kind, result.model_dump_json(), _now()),
        )
        self.conn.commit()

    # --- whole-entry verdict fast path ---
    def get_entry_verdict(self, entry_hash: str) -> Verdict | None:
        row = self.conn.execute(
            "SELECT verdict_json FROM entry_verdict_cache "
            "WHERE entry_hash=? AND backend=? AND pipeline_version=? AND model=? AND created_at>=?",
            (entry_hash, self.backend, self.pipeline_version, self.model, self.since),
        ).fetchone()
        return Verdict.model_validate_json(row["verdict_json"]) if row else None

    # @cpt-dod:cpt-referenceaudit-dod-identification-caching:p1
    def put_entry_verdict(self, entry_hash: str, verdict: Verdict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO entry_verdict_cache "
            "(entry_hash, backend, verdict_json, pipeline_version, model, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                entry_hash, self.backend, verdict.model_dump_json(), self.pipeline_version,
                self.model, _now(),
            ),
        )
        self.conn.commit()

    # --- DOI resolution cache (does doi.org's handle system know this DOI?) ---
    def get_doi_resolution(self, doi: str) -> bool | None:
        """Cached doi.org verdict for `doi`: True/False if known, None if never checked."""
        row = self.conn.execute(
            "SELECT resolves FROM doi_resolution_cache WHERE doi=? AND checked_at>=?",
            (doi, self.since),
        ).fetchone()
        return bool(row["resolves"]) if row else None

    def put_doi_resolution(self, doi: str, resolves: bool) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO doi_resolution_cache (doi, resolves, checked_at) "
            "VALUES (?,?,?)",
            (doi, 1 if resolves else 0, _now()),
        )
        self.conn.commit()

    # --- LLM decision cache (used from M4) ---
    def get_llm_decision(self, p_hash: str, kind: str) -> str | None:
        row = self.conn.execute(
            "SELECT result_json FROM llm_decision_cache "
            "WHERE prompt_hash=? AND kind=? AND model=? AND created_at>=?",
            (p_hash, kind, self.model, self.since),
        ).fetchone()
        return row["result_json"] if row else None

    def put_llm_decision(self, p_hash: str, kind: str, result_json: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO llm_decision_cache "
            "(prompt_hash, kind, model, result_json, created_at) VALUES (?,?,?,?,?)",
            (p_hash, kind, self.model, result_json, _now()),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
