"""Step 3 field-correctness checks — unit tests.

Deterministic-rule tests need no network/LLM; the escalation tests drive a fake in-memory LLM.
"""

from __future__ import annotations

import pytest

from reference_audit.cache.store import AuditCache
from reference_audit.config import AuditConfig
from reference_audit.fieldcheck import (
    _fold,
    _norm_pages,
    _pages_clean_range,
    deterministic_field_checks,
    finding_note,
    resolve_field_findings,
)
from reference_audit.models import (
    BibEntry,
    EntryType,
    FieldFinding,
    FieldJudgment,
    Identifiers,
    MatchedArtifact,
    SourceRecord,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _entry(**kw) -> BibEntry:
    raw = kw.pop("raw_fields", {})
    base = dict(
        key="k",
        entry_type=EntryType.ARTICLE,
        title="A Title",
        authors=["Author, A."],
        year=2020,
        venue="Some Journal",
        ids=Identifiers(doi="10.1/x"),
        raw_fields=raw,
    )
    base.update(kw)
    return BibEntry(**base)


def _rec(source="crossref", **kw) -> SourceRecord:
    return SourceRecord(source=source, **kw)


def _artifact(*records: SourceRecord) -> MatchedArtifact:
    recs = list(records)
    return MatchedArtifact(records=recs, versions=recs, best_record=recs[0] if recs else None)


def _by_field(checks) -> dict[str, object]:
    return {c.field: c for c in checks}


# ── normalization primitives ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "a,b",
    [
        ("Physics reports", "Physics Reports"),
        ("Clément", "Clement"),  # accents fold away (anyascii)
        ("Flow-{L}enia", "Flow-Lenia"),  # brace protection rejoins
        ("Annual  Review", "annual review"),
    ],
)
def test_fold_equates_formatting_variants(a, b):
    assert _fold(a) == _fold(b)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("30241--30251", "30241-30251"),
        ("E8678--E8687", "e8678-e8687"),
        ("139–-158", "139-158"),  # en-dash + hyphen
        ("185-–197", "185-197"),
        ("77-85", "77-85"),
        ("40", "40"),
        ("e2120037119", "e2120037119"),
    ],
)
def test_norm_pages_collapses_dashes(raw, expected):
    assert _norm_pages(raw) == expected


@pytest.mark.parametrize(
    "raw,clean",
    [
        ("30241--30251", True),
        ("E8678--E8687", True),
        ("139–-158", False),  # contains en-dash
        ("185-–197", False),
        ("77-85", False),  # single hyphen, not '--'
        ("2495–2504", False),  # bare en-dash
    ],
)
def test_pages_clean_range(raw, clean):
    assert _pages_clean_range(raw) is clean


# ── title ────────────────────────────────────────────────────────────────────


def test_title_brace_and_case_fold_ok():
    e = _entry(title="Flow-Lenia: Towards Open-Ended Evolution")
    art = _artifact(_rec(title="Flow-{L}enia: towards open-ended evolution"))
    title = _by_field(deterministic_field_checks(e, art))["title"]
    assert title.status == "ok"


def test_title_difference_escalates():
    e = _entry(title="A Study of Cats")
    art = _artifact(_rec(title="A Study of Dogs and Their Habits"))
    title = _by_field(deterministic_field_checks(e, art))["title"]
    assert title.status == "needs_llm" and title.needs_llm


# ── venue / journal ──────────────────────────────────────────────────────────


def test_venue_exact_ok():
    e = _entry(venue="Physics Reports")
    art = _artifact(_rec(venue="Physics Reports"))
    assert _by_field(deterministic_field_checks(e, art))["journal/venue"].status == "ok"


def test_venue_capitalization_is_formatting():
    e = _entry(venue="Physics reports")
    art = _artifact(_rec(venue="Physics Reports"))
    v = _by_field(deterministic_field_checks(e, art))["journal/venue"]
    assert v.status == "formatting"


def test_venue_dropped_word_escalates():
    # goldenfeld: 'Annual Review Condensed Matter Physics' drops 'of'
    e = _entry(venue="Annual Review Condensed Matter Physics")
    art = _artifact(_rec(venue="Annual Review of Condensed Matter Physics"))
    v = _by_field(deterministic_field_checks(e, art))["journal/venue"]
    assert v.status == "needs_llm"


def test_venue_unverifiable_when_canonical_blank():
    e = _entry(venue="Some Journal")
    art = _artifact(_rec(venue=""))
    v = _by_field(deterministic_field_checks(e, art))["journal/venue"]
    assert v.status == "unverifiable"


@pytest.mark.parametrize(
    "repo_venue",
    [
        "arXiv (Cornell University)",
        "bioRxiv",
        "CU Scholar (University of Colorado Boulder)",
        "NASA Technical Reports Server (NASA)",
        "Radboud Repository (Radboud University)",
    ],
)
def test_venue_against_preprint_or_repository_is_unverifiable(repo_venue):
    # matched a preprint/repository copy → the published venue can't be confirmed (never an 'error')
    e = _entry(venue="Proceedings of the IEEE Conference on Computer Vision and Pattern Recognition")
    art = _artifact(_rec(source="openalex", venue=repo_venue))
    v = _by_field(deterministic_field_checks(e, art))["journal/venue"]
    assert v.status == "unverifiable"
    assert v.needs_llm is False  # never escalates to the LLM


# ── year ─────────────────────────────────────────────────────────────────────


def test_year_match_ok():
    e = _entry(year=2020)
    art = _artifact(_rec(year=2020))
    assert _by_field(deterministic_field_checks(e, art))["year"].status == "ok"


def test_year_off_by_one_is_uncertain():
    e = _entry(year=2026)
    art = _artifact(_rec(year=2025))
    y = _by_field(deterministic_field_checks(e, art))["year"]
    assert y.status == "uncertain"


def test_year_off_by_two_is_error():
    e = _entry(year=2020)
    art = _artifact(_rec(year=2018))
    assert _by_field(deterministic_field_checks(e, art))["year"].status == "error"


def test_year_unverifiable_when_canonical_missing():
    e = _entry(year=2020)
    art = _artifact(_rec(year=None))
    assert _by_field(deterministic_field_checks(e, art))["year"].status == "unverifiable"


def test_book_year_gap_is_uncertain_not_error():
    # mabook: a 1976 original matched to a 2018 reprint — an edition gap, not a bib mistake
    e = _entry(entry_type=EntryType.BOOK, venue="", year=1976)
    art = _artifact(_rec(source="crossref", year=2018))
    y = _by_field(deterministic_field_checks(e, art))["year"]
    assert y.status == "uncertain"
    assert "edition" in y.detail


def test_preprint_cited_version_year_is_valid_not_uncertain():
    # kumar2024automating: .bib cites arXiv v1 (2024); canonical/latest version is 2025. The cited
    # year is the original submission year (encoded in 2412.*) → valid, not "needs review".
    e = _entry(
        entry_type=EntryType.MISC, venue="", year=2024,
        ids=Identifiers(arxiv_id="2412.17799"),
    )
    art = _artifact(_rec(source="semantic_scholar", year=2025))
    y = _by_field(deterministic_field_checks(e, art))["year"]
    assert y.status == "ok"
    assert "later version" in y.detail


def test_preprint_arxiv_doi_cited_version_year_is_valid():
    e = _entry(
        entry_type=EntryType.MISC, venue="", year=2024,
        ids=Identifiers(doi="10.48550/arxiv.2412.17799"),
    )
    art = _artifact(_rec(source="openalex", year=2025))
    assert _by_field(deterministic_field_checks(e, art))["year"].status == "ok"


def test_preprint_year_not_matching_submission_still_flagged():
    # Cited year (2020) is NOT the id-encoded submission year (2024) → a real discrepancy, flagged.
    e = _entry(
        entry_type=EntryType.MISC, venue="", year=2020,
        ids=Identifiers(arxiv_id="2412.17799"),
    )
    art = _artifact(_rec(source="semantic_scholar", year=2025))
    assert _by_field(deterministic_field_checks(e, art))["year"].status == "error"


def test_preprint_year_ahead_of_canonical_still_flagged():
    # Canonical (2024) is earlier than the cited year (2025): not a "newer version" case → flagged.
    e = _entry(
        entry_type=EntryType.MISC, venue="", year=2025,
        ids=Identifiers(arxiv_id="2412.17799"),
    )
    art = _artifact(_rec(source="semantic_scholar", year=2024))
    assert _by_field(deterministic_field_checks(e, art))["year"].status == "uncertain"


# ── volume ───────────────────────────────────────────────────────────────────


def test_volume_match_ok():
    e = _entry(raw_fields={"volume": "12"})
    art = _artifact(_rec(volume="12"))
    assert _by_field(deterministic_field_checks(e, art))["volume"].status == "ok"


def test_volume_mismatch_error():
    e = _entry(raw_fields={"volume": "9"})
    art = _artifact(_rec(volume="8"))
    assert _by_field(deterministic_field_checks(e, art))["volume"].status == "error"


def test_volume_absent_not_checked():
    e = _entry(raw_fields={})
    art = _artifact(_rec(volume="8"))
    assert "volume" not in _by_field(deterministic_field_checks(e, art))


def test_volume_leading_zero_ok():
    e = _entry(raw_fields={"volume": "043001"})
    art = _artifact(_rec(volume="43001"))
    assert _by_field(deterministic_field_checks(e, art))["volume"].status == "ok"


# ── number / issue ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("placeholder", ["", "-", "--", "n/a"])
def test_number_placeholder_is_error(placeholder):
    e = _entry(raw_fields={"number": placeholder})
    art = _artifact(_rec(issue="3"))
    n = _by_field(deterministic_field_checks(e, art))["number"]
    assert n.status == "error"
    assert "3" in n.detail  # canonical issue surfaced


def test_number_from_issue_field_value_compared():
    # wolpert: the entry carries the issue under the non-standard `issue` field
    e = _entry(raw_fields={"issue": "3"})
    art = _artifact(_rec(issue="3"))
    assert _by_field(deterministic_field_checks(e, art))["number"].status == "ok"


def test_number_mismatch_error():
    e = _entry(raw_fields={"number": "4"})
    art = _artifact(_rec(issue="3"))
    assert _by_field(deterministic_field_checks(e, art))["number"].status == "error"


def test_number_unverifiable_when_canonical_missing():
    e = _entry(raw_fields={"number": "4"})
    art = _artifact(_rec(issue=""))
    assert _by_field(deterministic_field_checks(e, art))["number"].status == "unverifiable"


# ── pages ────────────────────────────────────────────────────────────────────


def test_pages_clean_double_dash_ok():
    e = _entry(pages="30241--30251")
    art = _artifact(_rec(pages="30241-30251"))
    assert _by_field(deterministic_field_checks(e, art))["pages"].status == "ok"


def test_pages_letter_prefixed_range_ok():
    e = _entry(pages="E8678--E8687")
    art = _artifact(_rec(pages="E8678-E8687"))
    assert _by_field(deterministic_field_checks(e, art))["pages"].status == "ok"


@pytest.mark.parametrize("bad", ["139–-158", "185-–197", "77-85"])
def test_pages_odd_separator_is_formatting(bad):
    # same page numbers, non-canonical separator → a formatting nit, never an error
    canonical = bad.replace("–", "").replace("-", "-").replace("--", "-")
    e = _entry(pages=bad)
    art = _artifact(_rec(pages=_norm_pages(bad)))
    p = _by_field(deterministic_field_checks(e, art))["pages"]
    assert p.status == "formatting"


def test_pages_different_numbers_is_error():
    e = _entry(pages="185--197")
    art = _artifact(_rec(pages="185-200"))
    assert _by_field(deterministic_field_checks(e, art))["pages"].status == "error"


def test_pages_single_page_ok():
    e = _entry(pages="40")
    art = _artifact(_rec(pages="40"))
    assert _by_field(deterministic_field_checks(e, art))["pages"].status == "ok"


def test_pages_unverifiable_when_canonical_missing():
    e = _entry(pages="1--10")
    art = _artifact(_rec(pages=""))
    assert _by_field(deterministic_field_checks(e, art))["pages"].status == "unverifiable"


def test_pages_single_article_number_vs_range_is_error():
    # plantec: the canonical 'page' is a single article number (131); the entry inflated it into a
    # range '131--144', inventing an end page no source confirms.
    e = _entry(pages="131--144")
    art = _artifact(_rec(pages="131"))
    p = _by_field(deterministic_field_checks(e, art))["pages"]
    assert p.status == "error"
    assert "article number" in p.detail and "131" in p.detail


# ── publisher ────────────────────────────────────────────────────────────────


def test_publisher_typo_escalates():
    # gavrilets: 'Princeton Un iversity Press' — a split-word typo a rule cannot judge
    e = _entry(entry_type=EntryType.BOOK, venue="", publisher="Princeton Un iversity Press")
    art = _artifact(_rec(source="openlibrary", publisher="Princeton University Press"))
    p = _by_field(deterministic_field_checks(e, art))["publisher"]
    assert p.status == "needs_llm"


# ── canonical sourcing across records ─────────────────────────────────────────


def test_canonical_prefers_rich_source_and_fills_from_any_record():
    # S2 is the 'best' match but carries no pages/volume; crossref supplies them.
    e = _entry(raw_fields={"volume": "117"}, pages="30241--30251")
    art = _artifact(
        _rec(source="semantic_scholar", volume="", pages=""),
        _rec(source="crossref", volume="117", pages="30241-30251"),
    )
    checks = _by_field(deterministic_field_checks(e, art))
    assert checks["volume"].status == "ok"
    assert checks["volume"].sources == ["crossref"]
    assert checks["pages"].status == "ok"


# ── LLM escalation ───────────────────────────────────────────────────────────


class FakeLLM:
    def __init__(self, judgment):
        self.judgment = judgment
        self.calls = 0

    async def structured(self, system, user, schema_model, schema_name):
        self.calls += 1
        if self.judgment == "raise":
            from reference_audit.llm.client import LLMError

            raise LLMError("boom")
        return self.judgment

    async def aclose(self):
        pass


async def _resolve(entry, artifact, llm, cache=None):
    return await resolve_field_findings(entry, artifact, llm, AuditConfig(model="t"), cache)


async def test_llm_classifies_dropped_word_as_error():
    e = _entry(venue="Annual Review Condensed Matter Physics")
    art = _artifact(_rec(venue="Annual Review of Condensed Matter Physics"))
    llm = FakeLLM(FieldJudgment(classification="error", confidence="high", reason="dropped 'of'"))
    findings = {f.field: f for f in await _resolve(e, art, llm)}
    assert findings["journal/venue"].status == "error"
    assert findings["journal/venue"].via_llm is True
    assert llm.calls == 1


async def test_llm_classifies_abbreviation_as_formatting():
    e = _entry(venue="J. Theor. Biol.")
    art = _artifact(_rec(venue="Journal of Theoretical Biology"))
    llm = FakeLLM(
        FieldJudgment(classification="formatting_variant", confidence="high", reason="abbrev")
    )
    findings = {f.field: f for f in await _resolve(e, art, llm)}
    assert findings["journal/venue"].status == "formatting"


async def test_llm_low_confidence_error_becomes_uncertain():
    e = _entry(venue="Some Other Journal")
    art = _artifact(_rec(venue="Journal of Things"))
    llm = FakeLLM(FieldJudgment(classification="error", confidence="low", reason="maybe"))
    findings = {f.field: f for f in await _resolve(e, art, llm)}
    assert findings["journal/venue"].status == "uncertain"


async def test_llm_unavailable_marks_uncertain_not_silent():
    e = _entry(venue="Annual Review Condensed Matter Physics")
    art = _artifact(_rec(venue="Annual Review of Condensed Matter Physics"))
    findings = {f.field: f for f in await _resolve(e, art, None)}
    assert findings["journal/venue"].status == "uncertain"
    assert "LLM unavailable" in findings["journal/venue"].detail


async def test_llm_error_marks_uncertain():
    e = _entry(venue="Annual Review Condensed Matter Physics")
    art = _artifact(_rec(venue="Annual Review of Condensed Matter Physics"))
    findings = {f.field: f for f in await _resolve(e, art, FakeLLM("raise"))}
    assert findings["journal/venue"].status == "uncertain"


async def test_llm_decision_is_cached(tmp_path):
    cache = AuditCache(tmp_path / "c.db", model="t")
    e = _entry(venue="Annual Review Condensed Matter Physics")
    art = _artifact(_rec(venue="Annual Review of Condensed Matter Physics"))
    llm = FakeLLM(FieldJudgment(classification="error", confidence="high", reason="x"))
    await _resolve(e, art, llm, cache)
    assert llm.calls == 1
    await _resolve(e, art, llm, cache)  # served from cache
    assert llm.calls == 1
    cache.close()


# ── finding_note rendering ───────────────────────────────────────────────────


def test_consulted_sources_unions_underlying_sources():
    # A pooled representative records every source behind it; consulted_sources reports exactly what
    # the field checks compared against (so an 'unverifiable' names sources, not universal absence).
    from reference_audit.fieldcheck import consulted_sources

    rep = _rec(source="semantic_scholar", raw={"merged_from": ["crossref", "openalex"]})
    pub = _rec(source="publisher")
    assert consulted_sources(_artifact(rep, pub)) == ["crossref", "openalex", "publisher"]


def test_finding_note_error_mentions_values():
    f = FieldFinding(
        field="volume", bib_value="9", canonical_value="8", sources=["crossref"],
        status="error", detail="mismatch",
    )
    note = finding_note(f)
    assert "volume" in note and "9" in note and "8" in note and "crossref" in note


# ── pipeline integration (mocked Crossref) ───────────────────────────────────

import httpx  # noqa: E402
import respx  # noqa: E402

from reference_audit.pipeline import AuditPipeline  # noqa: E402
from reference_audit.sources.crossref import CrossrefAdapter  # noqa: E402

# Entry matches by DOI (→ exactly_one, no LLM needed), but volume is wrong and number is empty.
_BIB = (
    "@article{x, title={Quantum Foo}, author={Author, A.}, journal={Test Journal}, "
    "volume={9}, number={}, pages={1--10}, year={2020}, doi={10.1234/foo}}"
)
_CR_ITEM = {
    "DOI": "10.1234/foo",
    "title": ["Quantum Foo"],
    "author": [{"given": "A.", "family": "Author"}],
    "container-title": ["Test Journal"],
    "issued": {"date-parts": [[2020]]},
    "volume": "8",
    "issue": "3",
    "page": "1-10",
    "type": "journal-article",
}


@respx.mock
async def test_pipeline_field_check_flags_wrong_volume_and_empty_number(tmp_path):
    respx.get(url__regex=r"api\.crossref\.org/works/10\.1234/foo").mock(
        return_value=httpx.Response(200, json={"message": _CR_ITEM})
    )
    respx.get(url__regex=r"api\.crossref\.org/works\?").mock(
        return_value=httpx.Response(200, json={"message": {"items": []}})
    )
    bib = tmp_path / "r.bib"
    bib.write_text(_BIB, encoding="utf-8")
    pipe = AuditPipeline(
        AuditConfig(model="t"),
        adapters=[CrossrefAdapter(client=httpx.AsyncClient())],
        llm=None,
    )
    report = await pipe.run(None, bib)
    await pipe.aclose()

    audit = report.entries[0]
    assert audit.verdict.kind == "exactly_one"  # field check never disturbs the verdict
    by_field = {f.field: f for f in audit.field_findings}
    assert by_field["volume"].status == "error"
    assert by_field["number"].status == "error"
    assert by_field["pages"].status == "ok"  # '1--10' vs '1-10' is not a mistake
    assert by_field["year"].status == "ok"
    # actionable findings are surfaced as issues; benign ones are not
    assert any("volume" in i for i in audit.issues)
    assert any("number" in i for i in audit.issues)
