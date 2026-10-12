"""Cache behavior: only successes stored, model/pipeline gating, --fresh ignores old rows."""

from reference_audit.cache.store import AuditCache
from reference_audit.models import Identifiers, SourceQueryResult, SourceRecord, Verdict


def test_source_query_roundtrip_skips_errors(tmp_path):
    c = AuditCache(tmp_path / "c.db", model="m")
    ok = SourceQueryResult(
        source="crossref", query_kind="id",
        records=[SourceRecord(source="crossref", ids=Identifiers(doi="10.1/x"))],
    )
    c.put_source_query("h1", ok)
    got = c.get_source_query("h1", "crossref", "id")
    assert got is not None and len(got.records) == 1

    err = SourceQueryResult(source="crossref", query_kind="id", error="boom")
    c.put_source_query("h2", err)
    assert c.get_source_query("h2", "crossref", "id") is None  # errors never cached
    c.close()


def test_verdict_gated_by_model(tmp_path):
    v = Verdict(kind="exactly_one", confidence="high", rationale="r")
    c1 = AuditCache(tmp_path / "c.db", model="A", pipeline_version="1")
    c1.put_entry_verdict("h", v)
    assert c1.get_entry_verdict("h") is not None
    c1.close()

    c2 = AuditCache(tmp_path / "c.db", model="B", pipeline_version="1")
    assert c2.get_entry_verdict("h") is None  # model switch invalidates
    c2.close()


def test_verdict_gated_by_pipeline_version(tmp_path):
    v = Verdict(kind="none", confidence="high", rationale="r")
    c1 = AuditCache(tmp_path / "c.db", model="m", pipeline_version="1")
    c1.put_entry_verdict("h", v)
    c1.close()
    c2 = AuditCache(tmp_path / "c.db", model="m", pipeline_version="2")
    assert c2.get_entry_verdict("h") is None  # logic change invalidates
    c2.close()


def _fill(c: AuditCache, key: str) -> None:
    c.put_entry_verdict(key, Verdict(kind="none", confidence="high", rationale="r"))
    c.put_source_query(key, SourceQueryResult(source="crossref", query_kind="id", records=[]))
    c.put_llm_decision(key, "k", "{}")
    c.put_doi_resolution(key, True)


def _cached(c: AuditCache, key: str) -> list[bool]:
    return [
        c.get_entry_verdict(key) is not None,
        c.get_source_query(key, "crossref", "id") is not None,
        c.get_llm_decision(key, "k") is not None,
        c.get_doi_resolution(key) is not None,
    ]


def test_fresh_ignores_old_rows_without_deleting_them(tmp_path):
    """--fresh re-queries this run's entries but leaves a shared DB's other results alone (#6)."""
    path = tmp_path / "c.db"
    c = AuditCache(path, model="m")
    _fill(c, "mine")
    _fill(c, "other")
    c.close()

    fresh = AuditCache(path, model="m", fresh=True)
    assert _cached(fresh, "mine") == [False] * 4  # rows from before this run are not served
    _fill(fresh, "mine")
    assert _cached(fresh, "mine") == [True] * 4  # what this run wrote is
    fresh.close()

    later = AuditCache(path, model="m")
    assert _cached(later, "mine") == [True] * 4
    assert _cached(later, "other") == [True] * 4  # not wiped by the --fresh run
    later.close()
