"""Local ClickHouse backend: row → record mapping, id precedence, failures as errors, the preflight,
backend switching and backend-scoped caching. Offline (a fake connection) except the one live test,
which needs the local mirror and skips without it (REFERENCE_AUDIT_LIVE=1 makes it required)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date, datetime

import pytest

from reference_audit.cache.store import AuditCache
from reference_audit.config import AuditConfig
from reference_audit.models import BibEntry, EntryType, Identifiers, Verdict
from reference_audit.pipeline import AuditPipeline
from reference_audit.sources.clickhouse import (
    ClickHouseConnection,
    ClickHouseDblpAdapter,
    ClickHouseOpenAlexAdapter,
    ClickHouseQueryError,
    ClickHouseSemanticScholarAdapter,
    ClickHouseUnavailableError,
)
from reference_audit.sources.registry import build_default_adapters

CONFIG = AuditConfig(source_backend="clickhouse", clickhouse_password="")
TITLE = "Whose Opinions Do Language Models Reflect?"


class FakeConnection(ClickHouseConnection):
    """Answers queries from `routes` ({SQL substring: rows}); records every query and its parameters."""

    def __init__(self, routes: dict[str, list[dict]] | None = None, fail: str | None = None):
        super().__init__(CONFIG)
        self.routes = routes or {}
        self.fail = fail
        self.queries: list[tuple[str, dict | None]] = []

    async def query(self, sql: str, parameters: dict | None = None) -> list[dict]:
        self.queries.append((sql, parameters))
        if self.fail is not None and self.fail in sql:
            raise ClickHouseQueryError("clickhouse: TimeoutError: Code: 159 TIMEOUT_EXCEEDED")
        for needle, rows in self.routes.items():
            if needle in sql:
                return rows
        return []


def _entry(title: str = TITLE) -> BibEntry:
    return BibEntry(key="k", entry_type=EntryType.INPROCEEDINGS, title=title)


# ── Semantic Scholar ──────────────────────────────────────────────────────────

_S2_PAPER = {
    "corpusid": 257834040,
    "title": TITLE,
    "venue": "International Conference on Machine Learning",
    "year": 2023,
    "externalids": json.dumps({"DOI": None, "ArXiv": "2303.17548", "DBLP": "journals/corr/abs-2303-17548",
                               "CorpusId": "257834040"}),
    "citationcount": 512,
    "author_names": ["Shibani Santurkar", "Esin Durmus", ""],
}


def test_s2_search_maps_rows_like_the_api():
    ch = FakeConnection({
        "hasAllTokens": [{"id": 257834040, "t": TITLE, "c": 512}],
        "FROM s2ag.papers WHERE corpusid IN": [_S2_PAPER],
        "s2ag.abstracts": [{"corpusid": 257834040, "abstract": "Language models are used..."}],
    })
    res = asyncio.run(ClickHouseSemanticScholarAdapter(ch).search_by_metadata(_entry()))
    assert res.error is None and res.source == "semantic_scholar"
    (rec,) = res.records
    assert (rec.source, rec.source_native_id, rec.title, rec.year) == (
        "semantic_scholar", "257834040", TITLE, 2023
    )
    assert rec.venue == "International Conference on Machine Learning"
    assert rec.authors == ["Shibani Santurkar", "Esin Durmus"]
    assert rec.ids.arxiv_id == "2303.17548" and rec.ids.doi is None
    assert rec.abstract == "Language models are used..."
    search_sql, params = ch.queries[0]
    assert params["w"] == ["whose", "opinions", "do", "language", "models", "reflect"]
    assert "ORDER BY length(t), c DESC" in search_sql
    # the all-words hit is the exact title, so there is no leave-one-out retry
    assert sum("hasAllTokens" in q for q, _ in ch.queries) == 1


def test_a_misspelt_title_word_is_retried_with_each_word_left_out():
    typo = "Whose Opinions Do Language Modles Reflect?"
    ch = FakeConnection({
        "{w:Array(String)}": [],  # all words: the typo matches nothing
        "{w0:Array(String)}": [{"id": 257834040, "t": TITLE, "c": 512}],
        "FROM s2ag.papers WHERE corpusid IN": [_S2_PAPER],
    })
    res = asyncio.run(ClickHouseSemanticScholarAdapter(ch).search_by_metadata(_entry(typo)))
    assert [r.title for r in res.records] == [TITLE]
    relaxed_sql, params = ch.queries[1]
    assert "editDistance(lower(t), {cited:String})" in relaxed_sql
    assert params["cited"] == typo.lower()
    assert params["w4"] == ["whose", "opinions", "do", "language", "reflect"]  # "modles" left out
    assert len([k for k in params if k.startswith("w")]) == 6


def test_relaxed_hits_rank_ahead_of_inexact_all_words_hits():
    ch = FakeConnection({
        "{w:Array(String)}": [{"key": "a/long", "title": "Whose opinions do language models reflect? "
                               "A really large survey of polling methods across twenty countries",
                               "year": 2024, "authors": [], "dois": []}],
        "{w0:Array(String)}": [{"key": "conf/icml/S23", "title": TITLE, "year": 2023,
                                "authors": [], "dois": []}],
    })
    res = asyncio.run(ClickHouseDblpAdapter(ch).search_by_metadata(_entry(TITLE + " really")))
    assert [r.source_native_id for r in res.records] == ["conf/icml/S23", "a/long"]


def test_a_short_title_is_not_retried():
    ch = FakeConnection()
    asyncio.run(ClickHouseOpenAlexAdapter(ch).search_by_metadata(_entry("Deep Lerning")))
    assert sum("hasAllTokens" in q for q, _ in ch.queries) == 1


def test_s2_id_lookup_follows_the_api_precedence_and_lowercases():
    ch = FakeConnection({
        "paper_external_ids": [{"corpusid": 1}],
        "FROM s2ag.papers WHERE corpusid IN": [{**_S2_PAPER, "corpusid": 1}],
    })
    asyncio.run(ClickHouseSemanticScholarAdapter(ch).lookup_by_id(
        Identifiers(doi="10.1109/CVPR52729.2023.00373", arxiv_id="2303.17548")
    ))
    assert ch.queries[0][1] == {"t": "doi", "v": "10.1109/cvpr52729.2023.00373"}
    ch = FakeConnection()
    asyncio.run(ClickHouseSemanticScholarAdapter(ch).lookup_by_id(Identifiers(pmid="33225980")))
    assert ch.queries[0][1] == {"t": "pubmed", "v": "33225980"}


def test_a_failed_query_is_an_error_not_an_empty_result():
    ch = FakeConnection(fail="hasAllTokens")
    res = asyncio.run(ClickHouseSemanticScholarAdapter(ch).search_by_metadata(_entry()))
    assert res.records == [] and "TIMEOUT_EXCEEDED" in res.error


def test_a_title_without_searchable_words_is_an_error_without_a_query():
    ch = FakeConnection()
    for adapter in (ClickHouseSemanticScholarAdapter(ch), ClickHouseOpenAlexAdapter(ch),
                    ClickHouseDblpAdapter(ch)):
        res = asyncio.run(adapter.search_by_metadata(_entry("$\\mathcal{X}$ — 学习")))
        assert res.records == [] and "no searchable word" in res.error
    assert ch.queries == []


# ── OpenAlex ──────────────────────────────────────────────────────────────────

_OA_SLIM = {
    "id": "https://openalex.org/W4386065640",
    "doi": "https://doi.org/10.1109/cvpr52729.2023.00373",
    "ids": {"openalex": "https://openalex.org/W4386065640",
            "doi": "https://doi.org/10.1109/cvpr52729.2023.00373"},
    "title": "BiasAdv: Bias-Adversarial Augmentation for Model Debiasing",
    "publication_year": 2023,
    "type": "article",
    "cited_by_count": 24,
    "author_names": ["Jongin Lim", "Young‐Dong Kim"],
    "landing_page_url": "https://doi.org/10.1109/cvpr52729.2023.00373",
    "pdf_url": None,
    "source_name": "2023 IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)",
    "source_type": "conference",
}
_OA_FULL = {
    "id": "https://openalex.org/W4386065640",
    "volume": None, "issue": None, "first_page": "3832", "last_page": "3841",
    "abstract_inverted_index": json.dumps({"Neural": [0], "networks": [1], "overfit": [2]}),
}


def test_openalex_search_combines_slim_and_full_rows():
    ch = FakeConnection({
        "hasAllTokens": [{"id": _OA_SLIM["id"], "t": _OA_SLIM["title"], "c": 24}],
        "works_slim WHERE id IN": [_OA_SLIM],
        "openalex.works WHERE id IN": [_OA_FULL],
    })
    res = asyncio.run(ClickHouseOpenAlexAdapter(ch).search_by_metadata(_entry(_OA_SLIM["title"])))
    (rec,) = res.records
    assert rec.source == "openalex" and rec.ids.openalex == "W4386065640"
    assert rec.ids.doi == "10.1109/cvpr52729.2023.00373"
    assert rec.venue.startswith("2023 IEEE/CVF Conference")
    assert rec.pages == "3832--3841"
    assert rec.abstract == "Neural networks overfit"
    assert rec.is_preprint is False
    assert rec.version_links == ["https://doi.org/10.1109/cvpr52729.2023.00373"]


def test_openalex_best_oa_location_is_a_version_link():
    ch = FakeConnection({
        "hasAllTokens": [{"id": _OA_SLIM["id"], "t": _OA_SLIM["title"], "c": 24}],
        "works_slim WHERE id IN": [_OA_SLIM],
        "openalex.works WHERE id IN": [{**_OA_FULL,
                                        "oa_landing_page_url": "https://arxiv.org/abs/2303.17548",
                                        "oa_pdf_url": "https://arxiv.org/pdf/2303.17548"}],
    })
    adapter = ClickHouseOpenAlexAdapter(ch)
    (rec,) = asyncio.run(adapter.search_by_metadata(_entry(_OA_SLIM["title"]))).records
    assert rec.version_links == [
        "https://doi.org/10.1109/cvpr52729.2023.00373",
        "https://arxiv.org/abs/2303.17548", "https://arxiv.org/pdf/2303.17548",
    ]
    assert rec.ids.arxiv_id == "2303.17548"


def test_openalex_repository_source_is_a_preprint():
    ch = FakeConnection({
        "WHERE doi =": [{"id": "https://openalex.org/W1"}],
        "works_slim WHERE id IN": [{**_OA_SLIM, "id": "https://openalex.org/W1", "type": "preprint",
                                    "source_type": "repository", "source_name": "arXiv (Cornell University)"}],
    })
    res = asyncio.run(ClickHouseOpenAlexAdapter(ch).lookup_by_id(Identifiers(arxiv_id="2303.17548")))
    assert ch.queries[0][1] == {"v": "https://doi.org/10.48550/arxiv.2303.17548"}
    assert res.records[0].is_preprint is True
    assert res.records[0].pages == "" and res.records[0].abstract == ""  # no `works` row: absent, not guessed


def test_openalex_cited_work_id_is_looked_up_by_id():
    ch = FakeConnection()
    asyncio.run(ClickHouseOpenAlexAdapter(ch).lookup_by_id(Identifiers(openalex="W3034344071")))
    sql, params = ch.queries[0]
    assert "WHERE id =" in sql and params == {"v": "https://openalex.org/W3034344071"}


# ── DBLP ──────────────────────────────────────────────────────────────────────


def test_dblp_dump_rows_map_to_records():
    rows = [
        {"key": "conf/icml/SanturkarDLLLH23", "kind": "inproceedings", "publtype": "", "title": TITLE,
         "year": 2023, "venue": "ICML", "authors": ["Shibani Santurkar", "Bowen Baker 0001"], "dois": []},
        {"key": "journals/corr/abs-2303-17548", "kind": "article", "publtype": "informal", "title": TITLE + ".",
         "year": 2023, "venue": "CoRR", "authors": ["Shibani Santurkar"], "dois": ["10.48550/arXiv.2303.17548"]},
    ]
    ch = FakeConnection({"hasAllTokens": rows})
    res = asyncio.run(ClickHouseDblpAdapter(ch).search_by_metadata(_entry()))
    icml, corr = res.records
    assert icml.authors == ["Shibani Santurkar", "Bowen Baker"] and icml.is_preprint is False
    assert corr.title == TITLE and corr.is_preprint is True
    assert corr.ids.doi == "10.48550/arxiv.2303.17548" and corr.ids.arxiv_id == "2303.17548"
    assert "max(dump_date)" in ch.queries[0][0]


# ── preflight ─────────────────────────────────────────────────────────────────


class PreflightConnection(FakeConnection):
    def __init__(self, *, reachable=True, index_type="text", pending=0, coverage=date(2026, 9, 19)):
        super().__init__({
            "data_skipping_indices": [{"type": index_type}] if index_type else [],
            "system.mutations": [{"n": pending}],
            "SELECT 1": [{"1": 1}],
            " AS d FROM": [{"d": coverage}],
        }, fail=None if reachable else "SELECT 1")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"reachable": False}, "cannot be queried"),
        ({"index_type": None}, "ALTER TABLE s2ag.papers ADD INDEX IF NOT EXISTS idx_title_text"),
        ({"index_type": "tokenbf_v1"}, "has no full-text index"),
        ({"pending": 1}, "still being built"),
        ({"coverage": datetime(1970, 1, 1)}, "no usable coverage date"),
    ],
)
def test_preflight_refuses_an_unusable_mirror(kwargs, message):
    adapter = ClickHouseSemanticScholarAdapter(PreflightConnection(**kwargs))
    with pytest.raises(ClickHouseUnavailableError, match=message):
        asyncio.run(adapter.preflight())


def test_preflight_passes_and_runs_once_per_connection():
    ch = PreflightConnection()
    adapters = [ClickHouseSemanticScholarAdapter(ch), ClickHouseOpenAlexAdapter(ch)]

    async def both():
        for a in adapters:
            await a.preflight()

    asyncio.run(both())
    assert sum("SELECT 1" == q for q, _ in ch.queries) == 1
    assert [a.coverage_end for a in adapters] == [date(2026, 9, 19)] * 2


async def test_pipeline_run_stops_before_auditing_when_the_mirror_is_unusable(tmp_path):
    bib = tmp_path / "r.bib"
    bib.write_text("@article{a, title={A real title}, author={A. Author}, year={2021}}\n", "utf-8")
    pipe = AuditPipeline(
        AuditConfig(model="test", use_llm=False),
        adapters=[ClickHouseSemanticScholarAdapter(PreflightConnection(reachable=False))],
    )
    with pytest.raises(ClickHouseUnavailableError):
        await pipe.run(None, bib)
    await pipe.aclose()


# ── switching and caching ─────────────────────────────────────────────────────


def test_registry_switches_only_the_three_mirrored_sources():
    api = {a.name: a.backend for a in build_default_adapters(AuditConfig(source_backend="api"))}
    local = {a.name: a.backend for a in build_default_adapters(CONFIG)}
    assert set(api) == set(local)
    assert {n for n, b in local.items() if b == "clickhouse"} == {"semantic_scholar", "openalex", "dblp"}
    assert set(api.values()) == {"api"}
    assert local["crossref"] == "api"


def test_backends_never_share_cached_results(tmp_path):
    adapter = ClickHouseDblpAdapter(FakeConnection())
    assert adapter.name == "dblp" and adapter.cache_source == "dblp@clickhouse"

    v = Verdict(kind="none", confidence="high", rationale="r")
    api = AuditCache(tmp_path / "c.db", model="m", pipeline_version="1", backend="api")
    api.put_entry_verdict("h", v)
    api.close()
    local = AuditCache(tmp_path / "c.db", model="m", pipeline_version="1", backend="clickhouse")
    assert local.get_entry_verdict("h") is None
    local.put_entry_verdict("h", Verdict(kind="exactly_one", confidence="high", rationale="r"))
    local.close()
    api = AuditCache(tmp_path / "c.db", model="m", pipeline_version="1", backend="api")
    assert api.get_entry_verdict("h").kind == "none"  # both backends' verdicts are kept
    api.close()


def test_an_old_cache_is_migrated_and_its_verdicts_are_api_verdicts(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE entry_verdict_cache (entry_hash TEXT PRIMARY KEY, verdict_json TEXT NOT NULL, "
        "pipeline_version TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL);"
    )
    conn.execute(
        "INSERT INTO entry_verdict_cache VALUES (?,?,?,?,?)",
        ("h", Verdict(kind="none", confidence="high", rationale="r").model_dump_json(), "1", "m", "t"),
    )
    conn.commit()
    conn.close()
    api = AuditCache(path, model="m", pipeline_version="1", backend="api")
    assert api.get_entry_verdict("h").kind == "none"
    api.close()
    local = AuditCache(path, model="m", pipeline_version="1", backend="clickhouse")
    assert local.get_entry_verdict("h") is None
    local.close()


# ── live ──────────────────────────────────────────────────────────────────────


@pytest.mark.clickhouse
def test_live_mirror_finds_an_icml_paper_in_all_three_sources(require_clickhouse):
    config = require_clickhouse
    found: dict[str, list[str]] = {}

    async def run():
        connection = ClickHouseConnection(config)
        adapters = [cls(connection) for cls in (ClickHouseSemanticScholarAdapter,
                                                ClickHouseOpenAlexAdapter, ClickHouseDblpAdapter)]
        try:
            for a in adapters:
                res = await a.search_by_metadata(_entry())
                assert res.error is None, res.error
                found[a.name] = [r.title.rstrip(".") for r in res.records]
        finally:
            await connection.aclose()

    asyncio.run(run())
    assert all(TITLE in titles for titles in found.values()), found
