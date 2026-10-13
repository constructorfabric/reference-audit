"""Opt-in native-contract checks; reasoned failure and uncertainty remain explicit outcomes."""

import json
import os
from collections import Counter
from pathlib import Path

import pytest

import hallmark_bench as hb

from hallmark_cases import CASES, DATA, UNPARSEABLE, assert_native_outcome
from reference_audit.config import AuditConfig

pytestmark = [
    pytest.mark.hallmark_live,
    pytest.mark.skipif(
        os.environ.get("REFERENCE_AUDIT_HALLMARK_LIVE") != "1",
        reason="set REFERENCE_AUDIT_HALLMARK_LIVE=1 to audit the copied public corpus (network + LLM)",
    ),
]


@pytest.fixture(scope="module")
def live_audits(request, tmp_path_factory):
    config = AuditConfig(_env_file=DATA.parents[3] / ".env").model_copy(update={
        "check_fields": True, "check_alignment": False,
    })
    if not config.llm_enabled():
        pytest.fail("HALLMARK live tests require USE_LLM=true and OPENAI_API_KEY")
    workdir = tmp_path_factory.mktemp("hallmark-live")
    results = {}
    selected = [item.callspec.params["case"] for item in request.session.items
                if item.get_closest_marker("hallmark_live") and hasattr(item, "callspec")]

    def get_split(split):
        if split not in results:
            split_dir = workdir / split
            split_dir.mkdir()
            records = [c.record for c in selected if c.split == split]
            intact, broken = hb.round_trip(records, split_dir)
            assert set(broken) == UNPARSEABLE[split] & {r.bibtex_key for r in records}, (
                "unexpected HALLMARK parsing outcome"
            )
            audits = [hb.not_audited(k, "unparsed", why) for k, why in broken.items()]
            # Concurrent batches. An optional cache reuses successful queries and decisions; its
            # verdict fast path is still gated by pipeline version/model/backend.
            for offset in range(0, len(intact), 100):
                audits.extend(hb.audit_chunk(
                    intact[offset:offset + 100], split_dir / f"chunk-{offset}.bib",
                    config, Path(os.environ.get("REFERENCE_AUDIT_HALLMARK_CACHE") or
                                 workdir / "cache.db"),
                ))
            hb._write_jsonl(split_dir / "audits.jsonl", audits)
            assert len(audits) == len(records)
            assert {a.key for a in audits} == {r.bibtex_key for r in records}
            results[split] = {a.key: a for a in audits}
            # Binary mappings remain diagnostics for benchmark comparisons, never the native oracle.
            diagnostic = [{
                "key": c.record.bibtex_key, "upstream_label": c.upstream_label,
                "label_review": c.review.model_dump() if c.review else None,
                "native_expectation": c.native_expectation.model_dump() if c.native_expectation else None,
                "audit": results[split][c.record.bibtex_key].model_dump(),
                "strict_mapping": hb.predict(results[split][c.record.bibtex_key], "strict").model_dump(),
            } for c in selected if c.split == split]
            (split_dir / "diagnostics.json").write_text(
                json.dumps(diagnostic, indent=2, ensure_ascii=False), encoding="utf-8",
            )
            counts = Counter((a.verdict or "unresolved") if a.status == "audited" else a.status
                             for a in audits)
            print(f"\n{split}: {dict(counts)}; outcomes and diagnostics: {split_dir}")
        return results[split]

    return get_split


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_public_example_audit(case, live_audits):
    audit = live_audits(case.split)[case.record.bibtex_key]
    if case.record.bibtex_key in UNPARSEABLE[case.split]:
        assert audit.status == "unparsed" and "unbalanced braces" in audit.failure
        return
    assert_native_outcome(case, audit)
