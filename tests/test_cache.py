"""Cache behavior: only successes stored, model/pipeline gating, --fresh clears."""

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


def test_fresh_clears(tmp_path):
    c = AuditCache(tmp_path / "c.db", model="m")
    c.put_entry_verdict("h", Verdict(kind="none", confidence="high", rationale="r"))
    c.clear()
    assert c.get_entry_verdict("h") is None
    c.close()
