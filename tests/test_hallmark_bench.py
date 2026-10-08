"""The HALLMARK harness (`benchmarks/hallmark_bench.py`): .bib round-trip, verdict -> label mapping,
and per-record failure isolation. Offline: no source, LLM or HALLMARK process is involved."""

import pytest

import hallmark_bench as hb
from reference_audit.config import AuditConfig
from reference_audit.models import (
    AuditReport,
    BibEntry,
    EntryAudit,
    FieldFinding,
    MatchedArtifact,
    SourceRecord,
    Verdict,
)
from reference_audit.parsing.bib import parse_bib
from reference_audit.pipeline import AuditPipeline

AUTHORS = "Jonas M. K{\\\"u}bler and Andrew Arrasmith and M. Cerezo"


def _rec(key, fields, bib_type="inproceedings"):
    return hb.HallmarkRecord(bibtex_key=key, bibtex_type=bib_type, fields=fields)


# --- .bib round-trip ----------------------------------------------------------------------------


def test_round_trip_keeps_hallmark_shaped_records(tmp_path):
    records = [
        _rec("0a1b2c3d4e5f", {  # hex key starting with a digit
            "title": "Constitutional {AI}: Harmlessness from {AI} Feedback",
            "author": AUTHORS, "booktitle": "ICLR", "year": "2023",
            "doi": "10.48550/arXiv.2602.12271v1",
        }),
        _rec("b1c2d3e4f5a6", {
            "title": "Learning via $\\epsilon$-Greedy Exploration & Don&apos;t Panic",
            "author": "Francesco d&apos;Amore and Altch{\\'e} Smith", "year": "2021",
        }),
        _rec("c1d2e3f4a5b6", {
            "title": "Circular-symmetric correlation layer.", "author": "Bahar Azari",
            "journal": "Mach. Learn.", "year": "2023", "doi": "10.1007/S10994-022-06288-4",
        }, bib_type="article"),
    ]
    intact, broken = hb.round_trip(records, tmp_path)
    assert broken == {}
    assert [r.bibtex_key for r in intact] == [r.bibtex_key for r in records]

    hb.write_bib(records, tmp_path / "r.bib")
    by = {e.key: e for e in parse_bib(tmp_path / "r.bib")[0]}
    assert by["0a1b2c3d4e5f"].title == "Constitutional AI: Harmlessness from AI Feedback"
    assert by["0a1b2c3d4e5f"].authors[0] == "Jonas M. Kübler"
    assert by["0a1b2c3d4e5f"].venue == "ICLR"
    assert by["c1d2e3f4a5b6"].venue == "Mach. Learn."
    assert by["c1d2e3f4a5b6"].ids.doi == "10.1007/s10994-022-06288-4"


def test_round_trip_reports_an_unbalanced_entry_and_keeps_its_neighbours(tmp_path):
    good = {"title": "A real title", "author": "A. Author", "year": "2021", "booktitle": "ICML"}
    records = [
        _rec("aaaaaaaaaaaa", good),
        _rec("bbbbbbbbbbbb", {**good, "author": "Dario Amodei and Man{\\'e"}),  # truncated value
        _rec("cccccccccccc", good),
    ]
    intact, broken = hb.round_trip(records, tmp_path)
    assert [r.bibtex_key for r in intact] == ["aaaaaaaaaaaa", "cccccccccccc"]
    assert list(broken) == ["bbbbbbbbbbbb"]
    assert "unbalanced braces in author=" in broken["bbbbbbbbbbbb"]


# --- verdict -> HALLMARK label --------------------------------------------------------------------


def _entry_audit(kind, confidence="high", *, findings=(), bib_authors=None, matched_authors=None):
    authors = bib_authors or ["Ada Lovelace", "Alan Turing"]
    verdict = None
    if kind is not None:
        artifacts = []
        if kind == "exactly_one":
            best = SourceRecord(
                source="dblp", title="A real title", authors=matched_authors or authors,
                year=2021, venue="ICML",
            )
            artifacts = [MatchedArtifact(records=[best], best_record=best)]
        verdict = Verdict(kind=kind, artifacts=artifacts, confidence=confidence, rationale="r")
    return EntryAudit(
        entry=BibEntry(key="k", title="A real title", authors=authors, year=2021, venue="ICML"),
        verdict=verdict,
        field_findings=list(findings),
        issues=[] if verdict else ["source error: semantic_scholar 429"],
    )


def _finding(status, field="booktitle"):
    return FieldFinding(field=field, bib_value="NeurIPS", canonical_value="ICML", status=status)


def _labels(audit):
    compact = hb.compact(audit)
    predictions = {m: hb.predict(compact, m) for m in hb.MAPPINGS}
    return {m: (p.label, p.confidence, p.evaluated) for m, p in predictions.items()}


@pytest.mark.parametrize(
    ("audit", "identity", "strict"),
    [
        (_entry_audit("none", "high"), ("HALLUCINATED", 0.9), ("HALLUCINATED", 0.9)),
        (_entry_audit("none", "medium"), ("HALLUCINATED", 0.75), ("HALLUCINATED", 0.75)),
        (_entry_audit("exactly_one", "high"), ("VALID", 0.9), ("VALID", 0.9)),
        (_entry_audit("exactly_one", "medium"), ("VALID", 0.75), ("VALID", 0.75)),
        (_entry_audit("exactly_one", findings=[_finding("uncertain")]),
         ("VALID", 0.6), ("VALID", 0.6)),
        (_entry_audit("exactly_one", findings=[_finding("unverifiable")]),
         ("VALID", 0.6), ("UNCERTAIN", 0.5)),
        (_entry_audit("exactly_one", findings=[_finding("error")]),
         ("VALID", 0.6), ("HALLUCINATED", 0.7)),
        # an error outranks an unverifiable field
        (_entry_audit("exactly_one", findings=[_finding("unverifiable"), _finding("error", "year")]),
         ("VALID", 0.6), ("HALLUCINATED", 0.7)),
        # a cited author absent from the matched record is a metadata error
        (_entry_audit("exactly_one", matched_authors=["Ada Lovelace", "Grace Hopper"]),
         ("VALID", 0.6), ("HALLUCINATED", 0.7)),
        # cosmetic differences never move the label
        (_entry_audit("exactly_one", findings=[_finding("formatting")]),
         ("VALID", 0.9), ("VALID", 0.9)),
        (_entry_audit("multiple"), ("UNCERTAIN", 0.5), ("UNCERTAIN", 0.5)),
        (_entry_audit(None), ("UNCERTAIN", 0.5), ("UNCERTAIN", 0.5)),
    ],
)
def test_mapping_table(audit, identity, strict):
    assert _labels(audit) == {"identity": (*identity, True), "strict": (*strict, True)}


def test_unresolved_reason_carries_the_pipeline_issues():
    p = hb.predict(hb.compact(_entry_audit(None)), "identity")
    assert p.reason == "unresolved: source error: semantic_scholar 429"


def test_unparsed_record_is_reported_not_evaluated():
    audit = hb.not_audited("k", "unparsed", "unbalanced braces in author")
    for mapping in hb.MAPPINGS:
        p = hb.predict(audit, mapping)
        assert (p.label, p.evaluated) == ("UNCERTAIN", False)
        assert "unbalanced braces in author" in p.reason


async def test_crashed_entry_is_failed_and_not_evaluated(monkeypatch):
    """Pins `AUDIT_FAILED_PREFIX` to the message the pipeline really writes for a crashed entry."""

    async def boom(self, audit):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(AuditPipeline, "_audit_entry_inner", boom)
    pipe = AuditPipeline(AuditConfig(model="test"), adapters=[])
    audit = EntryAudit(entry=BibEntry(key="k", title="t"))
    await pipe._audit_entry(audit)
    await pipe.aclose()

    compact = hb.compact(audit)
    assert compact.status == "failed" and "kaboom" in compact.failure
    assert hb.predict(compact, "strict").evaluated is False


# --- per-record isolation in a chunk -------------------------------------------------------------


def _records():
    fields = {"title": "A real title", "author": "A. Author", "year": "2021"}
    return [_rec("aaaaaaaaaaaa", fields), _rec("bbbbbbbbbbbb", fields)]


def test_entry_missing_from_the_report_is_failed_alone(tmp_path, monkeypatch):
    def fake_run_audit(tex, bib, **kwargs):
        only = EntryAudit(
            entry=BibEntry(key="aaaaaaaaaaaa", title="A real title"),
            verdict=Verdict(kind="none", confidence="high"),
        )
        return AuditReport(entries=[only])

    monkeypatch.setattr(hb, "run_audit", fake_run_audit)
    rows = hb.audit_chunk(_records(), tmp_path / "c.bib", AuditConfig(), tmp_path / "c.db")
    assert [(r.key, r.status, r.verdict) for r in rows] == [
        ("aaaaaaaaaaaa", "audited", "none"),
        ("bbbbbbbbbbbb", "failed", None),
    ]
    assert rows[1].failure == "entry missing from the audit report"


def test_chunk_that_raises_reports_every_entry(tmp_path, monkeypatch):
    def fake_run_audit(tex, bib, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(hb, "run_audit", fake_run_audit)
    rows = hb.audit_chunk(_records(), tmp_path / "c.bib", AuditConfig(), tmp_path / "c.db")
    assert [r.status for r in rows] == ["failed", "failed"]
    assert all("OSError: disk full" in r.failure for r in rows)


# --- summary.md ---------------------------------------------------------------------------------


def _labeled(key, label, htype=None, tier=None, *, type_keys=True, **extra):
    row = {"bibtex_key": key, "label": label, "fields": {"title": f"Title {key}", "year": "2021"}}
    if type_keys:
        row |= {"hallucination_type": htype, "difficulty_tier": tier}
    return row | extra


def test_summary_handles_relabelled_rows_without_type_keys():
    # HALLMARK v1.2 rows relabelled HALLUCINATED -> VALID carry no hallucination_type /
    # difficulty_tier key at all, unlike other VALID rows (where both are null).
    labeled = [
        _labeled("v_plain", "VALID"),
        _labeled("v_relab", "VALID", type_keys=False,
                 relabeled_from="HALLUCINATED", relabel_reason="real paper"),
        _labeled("h_miss", "HALLUCINATED", "fabricated_doi", 1),
    ]
    assert "hallucination_type" not in labeled[1]
    label = {"v_plain": "VALID", "v_relab": "HALLUCINATED", "h_miss": "VALID"}
    predictions = {
        m: {k: hb.Prediction(bibtex_key=k, label=v, confidence=0.9, reason=f"why {k}")
            for k, v in label.items()}
        for m in hb.MAPPINGS
    }
    metrics = {"detection_rate": 0.0, "coverage": 1.0, "num_evaluated": 3}
    results = {m: {"conservative": metrics, "aggressive": metrics} for m in hb.MAPPINGS}
    meta = {
        "entries": 3, "counts": {}, "counts_before_retry": {}, "reference_audit_sha": "x",
        "pipeline_version": "0", "model": "m", "llm_enabled": True, "hallmark_sha": "y",
        "hallmark_version": "v1.2", "wall_seconds": 60, "from_cache": 0,
    }
    summary = hb.build_summary("dev_public", meta, labeled, predictions, results)
    assert "| VALID | — | 2 | 1 / 1 / 0 / 0 | 1 / 1 / 0 / 0 |" in summary
    assert "| fabricated_doi | 1 | 1 | 0 / 1 / 0 / 0 | 0 / 1 / 0 / 0 |" in summary
    # the false positive carries HALLMARK's own relabel history; a plain row carries none
    assert ("`v_relab` Title v_relab (2021) — why v_relab "
            "[HALLMARK relabelled HALLUCINATED → VALID: real paper]") in summary
    assert "### fabricated_doi (1 of 1)" in summary
    assert "| 1.000 |" in summary  # coverage is in the metrics table


# --- score, end to end through HALLMARK's own evaluator ------------------------------------------

_LABELED = hb.DEFAULT_HALLMARK_DIR / "data" / hb.HALLMARK_VERSION / "dev_public.jsonl"
_HALLMARK_BIN = hb.DEFAULT_HALLMARK_DIR / ".venv" / "bin" / "hallmark"


@pytest.mark.skipif(
    not (_LABELED.exists() and _HALLMARK_BIN.exists()),
    reason="needs the HALLMARK checkout and its venv (README, 'Benchmarking on HALLMARK')",
)
def test_score_runs_hallmark_on_a_run_with_a_not_audited_entry(tmp_path):
    """Both score bugs the first real run hit: a relabelled row without type keys, and an
    `evaluated=false` entry, which HALLMARK's `--strict` rejects."""
    import json

    rows = [json.loads(line) for line in _LABELED.read_text(encoding="utf-8").splitlines()]
    plain = [r for r in rows if r["label"] == "VALID" and "hallucination_type" in r]
    relabelled = next(r for r in rows if r["label"] == "VALID" and "hallucination_type" not in r)
    hallucinated = [r for r in rows if r["label"] == "HALLUCINATED"]
    chosen = [plain[0], relabelled, plain[1], hallucinated[0], hallucinated[1]]
    hallmark_dir = tmp_path / "hallmark"
    data = hallmark_dir / "data" / hb.HALLMARK_VERSION
    data.mkdir(parents=True)
    (data / "dev_public.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in chosen), encoding="utf-8"
    )

    def matched(key):
        return hb.CompactAudit(key=key, status="audited", verdict="exactly_one",
                               confidence="high", matched_source="dblp", matched_title="T")

    def no_match(key):
        return hb.CompactAudit(key=key, status="audited", verdict="none", confidence="high")

    k = [r["bibtex_key"] for r in chosen]
    audits = [
        matched(k[0]),                                            # true negative
        no_match(k[1]),                                           # false positive, relabelled
        hb.not_audited(k[2], "unparsed", "unbalanced braces"),    # not evaluated
        no_match(k[3]),                                           # detected
        matched(k[4]),                                            # missed
    ]
    out = tmp_path / "run"
    out.mkdir()
    hb._write_jsonl(out / "audits.jsonl", audits)
    (out / "run.json").write_text(json.dumps({
        "split": "dev_public", "entries": 5, "counts": {}, "counts_before_retry": {},
        "reference_audit_sha": "x", "pipeline_version": "0", "model": "m", "llm_enabled": True,
        "hallmark_sha": "y", "hallmark_version": hb.HALLMARK_VERSION, "wall_seconds": 60,
        "from_cache": 0,
    }), encoding="utf-8")

    hb.score(split="dev_public", hallmark_dir=hallmark_dir, hallmark_bin=_HALLMARK_BIN, out=out)

    result = json.loads((out / "eval.identity.json").read_text(encoding="utf-8"))["conservative"]
    assert (result["num_entries"], result["num_evaluated"]) == (5, 4)
    assert result["false_positive_rate"] == pytest.approx(0.5)
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "HALLMARK relabelled HALLUCINATED → VALID" in summary
    assert f"`{k[4]}`" in summary.split("## Misses")[1]


def test_resumed_audit_keeps_its_start_and_adds_up_wall_time(tmp_path, monkeypatch):
    import json

    def fake_run_audit(tex, bib, **kwargs):
        return AuditReport(entries=[
            EntryAudit(entry=BibEntry(key=k, title="A real title"),
                       verdict=Verdict(kind="none", confidence="high"))
            for k in ("aaaaaaaaaaaa", "bbbbbbbbbbbb")
        ])

    monkeypatch.setattr(hb, "run_audit", fake_run_audit)
    data = tmp_path / "hallmark" / "data" / hb.HALLMARK_VERSION
    data.mkdir(parents=True)
    (data / "dev_public_blind.jsonl").write_text(
        "".join(r.model_dump_json() + "\n" for r in _records()), encoding="utf-8"
    )
    out = tmp_path / "run"

    def run():
        hb.audit(split="dev_public", hallmark_dir=tmp_path / "hallmark", out=out,
                 cache=tmp_path / "c.db", limit=0, seed=0, chunk_size=200, retry_unresolved=0,
                 no_llm=True, backend="api")
        return json.loads((out / "run.json").read_text(encoding="utf-8"))

    first = run()
    (out / "run.json").write_text(
        json.dumps(first | {"started_at": "first-start", "wall_seconds": 1800}), encoding="utf-8"
    )
    resumed = run()
    assert resumed["started_at"] == "first-start"
    assert resumed["wall_seconds"] >= 1800  # the resumed invocation adds to, not replaces, the total
