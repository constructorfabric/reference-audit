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
    check_book_edition_fields,
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


# ── book edition fields (grounded in the cited Open Library edition) ──────────


async def test_book_year_ok_against_matched_edition():
    # The cited 1976 edition checked against the 1976 OL edition — no false 'needs review'.
    e = _entry(entry_type=EntryType.BOOK, venue="", year=1976,
               publisher="W. A. Benjamin, Advanced Book Program")
    edition = _rec(source="openlibrary", year=1976,
                   publisher="W. A. Benjamin, Advanced Book Program")
    findings = {
        f.field: f
        for f in await check_book_edition_fields(e, edition, None, AuditConfig(model="t"), None)
    }
    assert findings["year"].status == "ok"
    assert findings["publisher"].status == "ok"


async def test_book_publisher_typo_still_escalates_against_edition():
    # gavrilets: the cited 2004 edition's publisher carries a split-word typo; grounding the canonical
    # in the matched edition must still catch it (LLM escalation), not silently pass.
    e = _entry(entry_type=EntryType.BOOK, venue="", year=2004,
               publisher="Princeton Un iversity Press")
    edition = _rec(source="openlibrary", year=2004, publisher="Princeton University Press")
    llm = FakeLLM(FieldJudgment(classification="error", confidence="high", reason="split word"))
    findings = {
        f.field: f
        for f in await check_book_edition_fields(e, edition, llm, AuditConfig(model="t"), None)
    }
    assert findings["publisher"].status == "error"
    assert findings["publisher"].via_llm is True


async def test_book_no_publisher_field_only_checks_year():
    e = _entry(entry_type=EntryType.BOOK, venue="", year=1976, publisher="")
    edition = _rec(source="openlibrary", year=1976, publisher="Whoever")
    findings = await check_book_edition_fields(e, edition, None, AuditConfig(model="t"), None)
    assert [f.field for f in findings] == ["year"]


async def test_book_publisher_shortened_imprint_is_formatting_no_llm():
    # mabook: 'W. A. Benjamin' is a shortened form of the edition's fuller imprint — a formatting
    # nit, settled deterministically (no LLM call needed) rather than flagged as wrong.
    e = _entry(entry_type=EntryType.BOOK, venue="", year=1976, publisher="W. A. Benjamin")
    edition = _rec(source="openlibrary", year=1976,
                   publisher="W. A. Benjamin, Advanced Book Program")
    llm = FakeLLM(FieldJudgment(classification="error", confidence="high", reason="should not run"))
    findings = {
        f.field: f
        for f in await check_book_edition_fields(e, edition, llm, AuditConfig(model="t"), None)
    }
    assert findings["publisher"].status == "formatting"
    assert llm.calls == 0


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


# ── the version the entry cites (members of a pooled record) ────────────────


def _pooled(*members: SourceRecord) -> MatchedArtifact:
    from reference_audit.matching.pool import pool_candidates

    (rep,) = pool_candidates(list(members))
    return _artifact(rep)


_AUTH = ["Tonghan Wang", "Tarun Gupta", "Anuj Mahajan"]


def _arxiv_copy(**kw) -> SourceRecord:
    base = dict(source="openalex", title="RODE", authors=_AUTH, year=2020, citation_count=40,
                venue="arXiv (Cornell University)",
                ids=Identifiers(doi="10.48550/arxiv.2010.01523", arxiv_id="2010.01523"))
    return _rec(**(base | kw))


def test_a_conference_citation_is_checked_against_the_conference_record():
    # HALLMARK fbc5d48e8551: the pooled record carried OpenAlex's arXiv venue and year, so ICLR was
    # "unverifiable" and 2021 "differed" from 2020, though DBLP had ICLR 2021.
    e = _entry(title="RODE", authors=_AUTH, venue="ICLR", year=2021, ids=Identifiers())
    art = _pooled(_arxiv_copy(), _rec("dblp", title="RODE.", authors=_AUTH, venue="ICLR", year=2021))
    checks = _by_field(deterministic_field_checks(e, art))
    assert (checks["journal/venue"].status, checks["journal/venue"].sources) == ("ok", ["dblp"])
    assert checks["year"].status == "ok"


def test_a_preprint_citation_is_checked_against_the_preprint():
    e = _entry(title="RODE", authors=_AUTH, venue="arXiv preprint arXiv:2010.01523", year=2020,
               ids=Identifiers())
    art = _pooled(_arxiv_copy(), _rec("dblp", title="RODE.", authors=_AUTH, venue="ICLR", year=2021))
    checks = _by_field(deterministic_field_checks(e, art))
    assert checks["year"].status == "ok"                       # the preprint's year, not ICLR's
    assert checks["journal/venue"].status == "needs_llm"       # compared, not "unverifiable"


def test_a_later_journal_version_is_not_the_canonical_venue():
    # HALLMARK b683f8f34292: an ICML 2022 paper pooled with its 2025 journal version.
    a = ["Dimitris Fotakis", "Alkis Kalavasis", "Eleni Psaroudaki"]
    e = _entry(title="Label Ranking through Nonparametric Regression", authors=a, venue="ICML",
               year=2022, ids=Identifiers())
    art = _pooled(
        _rec("crossref", title="Label Ranking through Nonparametric Regression", authors=a,
             venue="Theory of Computing Systems", year=2025, ids=Identifiers(doi="10.1007/x"),
             citation_count=3),
        _rec("dblp", title="Label Ranking through Nonparametric Regression.", authors=a,
             venue="ICML", year=2022),
    )
    checks = _by_field(deterministic_field_checks(e, art))
    assert checks["journal/venue"].status == "ok"
    assert checks["year"].status == "ok"


def test_the_title_comes_from_the_published_version():
    # HALLMARK d541bf3fa5b9: the arXiv title was "...for Zero-shot Image Classification".
    a = ["Junnan Li", "Silvio Savarese", "Steven C. H. Hoi"]
    t = "Masked Unsupervised Self-training for Label-free Image Classification"
    e = _entry(title=t, authors=a, venue="ICLR", year=2023, ids=Identifiers())
    arxiv = Identifiers(doi="10.48550/arxiv.2206.02967", arxiv_id="2206.02967")
    art = _pooled(
        _arxiv_copy(title="Masked Unsupervised Self-training for Zero-shot Image Classification",
                    authors=a, year=2022, ids=arxiv),
        _rec("dblp", title=t + ".", authors=a, venue="ICLR", year=2023,
             ids=Identifiers(arxiv_id="2206.02967")),
    )
    assert _by_field(deterministic_field_checks(e, art))["title"].status == "ok"


def test_a_venue_only_preprint_copies_have_stays_unverifiable():
    e = _entry(title="RODE", authors=_AUTH, venue="NeurIPS", year=2020, ids=Identifiers())
    art = _pooled(_arxiv_copy(), _rec("dblp", title="RODE.", authors=_AUTH, venue="CoRR", year=2020,
                                      ids=Identifiers(arxiv_id="2010.01523")))
    v = _by_field(deterministic_field_checks(e, art))["journal/venue"]
    assert v.status == "unverifiable"
    assert "every source has only a preprint/repository copy" in v.detail


# ── authors ──────────────────────────────────────────────────────────────────


def test_a_cited_author_on_no_source_record_is_an_error():
    # Flamingo (HALLMARK a24129d1c5e5): 'João Carreira' passed as a near-namesake of 'Ricardo
    # Barreira' under the surname-only fuzzy match.
    e = _entry(authors=["Jean-Baptiste Alayrac", "Ricardo Barreira", "João Carreira"])
    art = _artifact(_rec("dblp", authors=["Jean-Baptiste Alayrac", "Jeff Donahue", "Pauline Luc",
                                          "Ricardo Barreira"]))
    a = _by_field(deterministic_field_checks(e, art))["author"]
    assert a.status == "error"
    assert a.detail.endswith("in any source: João Carreira")


def test_every_source_record_is_consulted_for_authors():
    # MSDN (HALLMARK a4a90253cc4d): S2 lists 'Wenhan Wang' for Wenhan Yang; DBLP has him right.
    e = _entry(authors=["Shiming Chen", "Wenhan Yang"], ids=Identifiers())
    art = _pooled(
        _rec("semantic_scholar", title="A Title", authors=["Shiming Chen", "Wenhan Wang"],
             ids=Identifiers(doi="10.1109/x")),
        _rec("dblp", title="A Title", authors=["Shiming Chen", "Wenhan Yang"],
             ids=Identifiers(doi="10.1109/x")),
    )
    assert _by_field(deterministic_field_checks(e, art))["author"].status == "ok"


def test_name_order_and_compound_surnames_are_the_same_people():
    e = _entry(authors=["Tian Li", "Lierni Sestorain", "Carlos Riquelme Ruiz", "Guo-Sen Xie"])
    art = _artifact(_rec("openalex", authors=[
        "Li Tian", "Sestorain Saralegui, Lierni", "Carlos Riquelme", "Guosen Xie"]))
    assert _by_field(deterministic_field_checks(e, art))["author"].status == "ok"


def test_a_replaced_author_list_is_checked_even_when_the_record_is_shorter():
    # HALLMARK b9e0c641d08e: 8 cited authors, none of them on the 5-author paper. The old length
    # guard read the shorter record as truncated and skipped the check.
    cited = ["Jianan Zhao", "Meng Qu", "Chaozhuo Li", "Hao Yan", "Qian Liu", "Rui Li", "Xing Xie",
             "Jian Tang"]
    e = _entry(authors=cited)
    art = _artifact(_rec("semantic_scholar", authors=[
        "Yiting Cheng", "Fangyun Wei", "Jianmin Bao", "Dong Chen", "Wenqian Zhang"]))
    assert _by_field(deterministic_field_checks(e, art))["author"].status == "error"


def test_authors_past_the_end_of_a_possibly_truncated_record_are_unverifiable():
    cited = ["Ada Lovelace", "Alan Turing", "Grace Hopper", "Edsger Dijkstra", "Donald Knuth",
             "Barbara Liskov", "John McCarthy", "Frances Allen", "Tony Hoare", "Leslie Lamport"]
    e = _entry(authors=cited)
    art = _artifact(_rec("openalex", authors=cited[:4]))  # the citation's leading part
    a = _by_field(deterministic_field_checks(e, art))["author"]
    assert a.status == "unverifiable"
    assert a.detail.startswith("every source lists only the first 4 of the 10 cited authors")


@pytest.mark.parametrize(("mode", "status"), [("ignore", "ok"), ("warn", "uncertain"),
                                              ("error", "error")])
def test_a_partial_author_list_follows_the_option(mode, status):
    full = ["Yining Wang", "Akshay Krishnamurthy", "Sivaraman Balakrishnan", "Aarti Singh"]
    art = _artifact(_rec("dblp", authors=full))
    partial = _by_field(deterministic_field_checks(
        _entry(authors=full[:2]), art, partial_authors=mode))["author"]
    assert partial.status == status
    if status != "ok":
        assert "omits 2 of the work's 4 authors" in partial.detail
    marked = _by_field(deterministic_field_checks(
        _entry(authors=[*full[:2], "others"]), art, partial_authors=mode))["author"]
    assert marked.status == "ok"  # 'and others' says the list is shortened


# ── the LLM's context is the matched work ────────────────────────────────────


async def test_the_field_check_llm_sees_the_matched_work_not_the_entry():
    # HALLMARK ba8fe5817a21: told the entry's title was "the same work, confirmed by identifier",
    # the LLM judged 'Resilient' vs 'Robust' Dynamic Radiance Fields a formatting difference.
    seen: list[str] = []

    class CapturingLLM:
        async def structured(self, system, user, schema_model, schema_name):
            seen.append(user)
            return FieldJudgment(classification="error", confidence="high", reason="r")

    e = _entry(title="Resilient Dynamic Radiance Fields", venue="CVPR")
    art = _artifact(_rec("crossref", title="Robust Dynamic Radiance Fields", venue="CVPR",
                         authors=["Author, A."]))
    findings = await resolve_field_findings(e, art, CapturingLLM(), AuditConfig(model="t"), None)
    (prompt,) = seen
    context = prompt.split("CONTEXT", 1)[1]
    assert "Robust Dynamic Radiance Fields" in context
    assert "Resilient" not in context
    assert next(f for f in findings if f.field == "title").status == "error"


# ── one source's defect is outvoted ──────────────────────────────────────────


def test_a_title_most_sources_agree_on_beats_a_more_authoritative_defect():
    # HALLMARK db9d82ff3f94: OpenAlex titles arXiv:2212.08073 "Affective Coherence Monitoring for
    # Transformer-Based Language Models"; S2, arXiv and DBLP have Constitutional AI. No member is the
    # cited ICLR version, so the preprint copies form one group, and OpenAlex outranks the others.
    t = "Constitutional AI: Harmlessness from AI Feedback"
    a = ["Yuntao Bai", "Saurav Kadavath", "Sandipan Kundu"]
    ids = Identifiers(doi="10.48550/arxiv.2212.08073", arxiv_id="2212.08073")
    e = _entry(title=t, authors=a, venue="ICLR", year=2023, ids=Identifiers())
    art = _pooled(
        _rec("openalex", title="Affective Coherence Monitoring for Transformer-Based Language Models",
             authors=a, year=2022, venue="arXiv (Cornell University)", ids=ids, citation_count=900),
        _rec("semantic_scholar", title=t, authors=a, year=2022, venue="arXiv.org", ids=ids),
        _rec("arxiv", title=t, authors=a, year=2022, ids=ids, is_preprint=True),
        _rec("dblp", title=t + ".", authors=a, year=2022, venue="CoRR", ids=ids),
    )
    title = _by_field(deterministic_field_checks(e, art))["title"]
    assert title.status == "ok"
    assert title.sources == ["arxiv", "dblp", "semantic_scholar"]


def test_the_vote_stays_within_the_cited_version():
    # Two sources for a 2025 journal version must not outvote the one record of the cited ICML 2022
    # paper: the vote is within the first group (kind, year), not across versions.
    a = ["Dimitris Fotakis", "Alkis Kalavasis", "Eleni Psaroudaki"]
    t = "Label Ranking through Nonparametric Regression"
    doi = Identifiers(doi="10.1007/x")
    e = _entry(title=t, authors=a, venue="ICML", year=2022, ids=Identifiers())
    art = _pooled(
        _rec("crossref", title=t, authors=a, venue="Theory of Computing Systems", year=2025, ids=doi),
        _rec("openalex", title=t, authors=a, venue="Theory of Computing Systems", year=2025, ids=doi),
        _rec("dblp", title=t + ".", authors=a, venue="ICML", year=2022),
    )
    checks = _by_field(deterministic_field_checks(e, art))
    assert (checks["journal/venue"].status, checks["year"].status) == ("ok", "ok")


async def test_the_llm_context_carries_the_title_most_sources_agree_on():
    seen: list[str] = []

    class CapturingLLM:
        async def structured(self, system, user, schema_model, schema_name):
            seen.append(user)
            return FieldJudgment(classification="error", confidence="high", reason="r")

    t = "Constitutional AI: Harmlessness from AI Feedback"
    ids = Identifiers(doi="10.48550/arxiv.2212.08073", arxiv_id="2212.08073")
    e = _entry(title="Constitutional AI: Harmless AI Feedback", authors=["Yuntao Bai"], venue="ICLR",
               year=2022, ids=Identifiers())
    art = _pooled(
        _rec("openalex", title="Affective Coherence Monitoring", authors=["Yuntao Bai"], year=2022,
             ids=ids, citation_count=900),
        _rec("semantic_scholar", title=t, authors=["Yuntao Bai"], year=2022, ids=ids),
        _rec("dblp", title=t, authors=["Yuntao Bai"], year=2022, ids=ids),
    )
    await resolve_field_findings(e, art, CapturingLLM(), AuditConfig(model="t"), None)
    context = seen[0].split("CONTEXT", 1)[1]
    assert t in context and "Affective" not in context
