"""DBLP: SPARQL row folding + the adapter + an end-to-end verify (mocked HTTP).

Regression anchors: the premier ML venues mint no DOI and are cited only by a proceedings URL —
`pmlr-v202-santurkar23a` ("Whose Opinions Do Language Models Reflect?", ICML/PMLR), with siblings at
NeurIPS and ICLR. The article-centric aggregators cover them thinly; DBLP indexes them exactly, so a
URL-only @inproceedings reaches a deterministic verdict.

DBLP is read through its SPARQL endpoint because the search API answers automated clients with a
bot-challenge HTML page; `tests/fixtures/dblp/` holds a recorded search + details response.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from reference_audit.models import BibEntry, EntryType
from reference_audit.sources.dblp import DblpAdapter, title_search_words
from reference_audit.sources.normalize import dblp_sparql_to_records

_ENDPOINT = "https://sparql.dblp.org/sparql"
_RECORDED = json.loads(
    (Path(__file__).parent / "fixtures" / "dblp" / "santurkar23.sparql.json").read_text("utf-8")
)
_SEARCH, _DETAILS = _RECORDED["search"], _RECORDED["details"]
_TITLE = "Whose Opinions Do Language Models Reflect?"
_ICML = "https://dblp.org/rec/conf/icml/SanturkarDLLLH23"


def _rows(*rows: dict) -> list[dict]:
    """SPARQL JSON bindings from plain {var: value} dicts."""
    return [{k: {"type": "literal", "value": v} for k, v in r.items()} for r in rows]


def _entry(title: str = _TITLE) -> BibEntry:
    return BibEntry(key="k", entry_type=EntryType.INPROCEEDINGS, title=title)


def _recorded_endpoint(request: httpx.Request) -> httpx.Response:
    query = request.url.params.get("query", "")
    return httpx.Response(200, json=_DETAILS if "VALUES" in query else _SEARCH)


# ── folding SPARQL rows into records ──────────────────────────────────────────


def test_recorded_rows_fold_into_the_icml_paper_and_its_corr_preprint():
    records = dblp_sparql_to_records(
        _SEARCH["results"]["bindings"] + _DETAILS["results"]["bindings"]
    )
    assert [r.source_native_id for r in records] == [
        "conf/icml/SanturkarDLLLH23",
        "journals/corr/abs-2303-17548",
    ]
    icml, corr = records
    assert icml.source == "dblp"
    assert icml.title == _TITLE
    assert (icml.year, icml.venue, icml.pages) == (2023, "ICML", "29971-30004")
    # signatures arrive out of order in the recorded response; the record follows signatureOrdinal
    assert icml.authors == [
        "Shibani Santurkar", "Esin Durmus", "Faisal Ladhak",
        "Cinoo Lee", "Percy Liang", "Tatsunori Hashimoto",
    ]
    # the primary document page (the very URL the .bib cites) is kept as the record URL
    assert icml.ids.url == "https://proceedings.mlr.press/v202/santurkar23a.html"
    assert icml.ids.doi is None and icml.is_preprint is False
    # dblp:Informal (CoRR) is a preprint; its DOI and arXiv id are recovered
    assert corr.is_preprint is True
    assert corr.ids.doi == "10.48550/arxiv.2303.17548"
    assert corr.ids.arxiv_id == "2303.17548"


def test_homonym_number_and_title_period_are_stripped():
    rows = _rows(
        {"publ": "https://dblp.org/rec/x/1", "title": "X."},
        {"publ": "https://dblp.org/rec/x/1", "ordinal": "1", "name": "Bowen Baker 0001"},
        {"publ": "https://dblp.org/rec/x/1", "year": "2024"},
    )
    (rec,) = dblp_sparql_to_records(rows)
    assert rec.authors == ["Bowen Baker"]
    assert rec.title == "X"
    assert rec.year == 2024


def test_multi_valued_property_is_the_same_whatever_the_row_order():
    base = {"publ": "https://dblp.org/rec/x/1", "title": "T"}
    a = _rows(base, {"publ": base["publ"], "venue": "NeurIPS"}, {"publ": base["publ"], "venue": "CoRR"})
    b = [a[0], a[2], a[1]]
    assert dblp_sparql_to_records(a)[0].venue == dblp_sparql_to_records(b)[0].venue == "CoRR"


def test_doi_comes_from_the_document_page_when_dblp_doi_is_absent():
    rows = _rows(
        {"publ": "https://dblp.org/rec/x/1", "title": "T"},
        {"publ": "https://dblp.org/rec/x/1", "ee": "https://doi.org/10.1109/CVPR52729.2023.00373"},
    )
    assert dblp_sparql_to_records(rows)[0].ids.doi == "10.1109/cvpr52729.2023.00373"


# ── the words sent to DBLP's word index ───────────────────────────────────────


def test_search_words_drop_latex_and_non_ascii_words():
    assert title_search_words(
        "Understanding Deep Neural Function Approximation via $\\epsilon$-Greedy Exploration"
    ) == ["understanding", "deep", "neural", "function", "approximation", "via", "greedy",
          "exploration"]
    assert title_search_words("Sparks of AGI: Early experiments with GPT-4") == [
        "sparks", "of", "agi", "early", "experiments", "with", "gpt", "4"
    ]
    # A non-ASCII word ("Kübler", as parse_bib decodes K{\"u}bler) may be indexed differently from
    # how the .bib spells it, and every word must match, so it is left out of the search.
    assert title_search_words("The Kübler method_2") == ["the", "method", "2"]


# ── adapter ───────────────────────────────────────────────────────────────────


@respx.mock
async def test_search_by_metadata_runs_search_then_details():
    queries: list[str] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params.get("query", ""))
        assert request.headers["accept"] == "application/sparql-results+json"
        return _recorded_endpoint(request)

    respx.get(url__startswith=_ENDPOINT).mock(side_effect=_capture)
    a = DblpAdapter(client=httpx.AsyncClient())
    res = await a.search_by_metadata(_entry())
    await a.aclose()
    assert res.error is None
    assert [r.source_native_id for r in res.records][0] == "conf/icml/SanturkarDLLLH23"
    search, details = queries
    assert 'ql:contains-word "whose opinions do language models reflect"' in search
    assert "ORDER BY STRLEN(?title) LIMIT 10" in search
    assert f"<{_ICML}>" in details


@respx.mock
async def test_no_hits_is_empty_not_error_and_skips_details():
    route = respx.get(url__startswith=_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"head": {"vars": []}, "results": {"bindings": []}})
    )
    a = DblpAdapter(client=httpx.AsyncClient())
    res = await a.search_by_metadata(_entry("Nonexistent"))
    await a.aclose()
    assert res.records == [] and res.error is None
    assert route.call_count == 1


@respx.mock
async def test_bot_challenge_page_is_an_error_not_absent():
    """Regression: dblp.org served an Anubis 'Making sure you're not a bot!' HTML page (HTTP 200)
    to automated clients. Such a page must surface as an error (retried), never as 'no hits'."""
    respx.get(url__startswith=_ENDPOINT).mock(
        return_value=httpx.Response(
            200, text="<!doctype html><title>Making sure you're not a bot!</title>",
            headers={"content-type": "text/html"},
        )
    )
    a = DblpAdapter(client=httpx.AsyncClient())
    res = await a.search_by_metadata(_entry())
    await a.aclose()
    assert res.records == [] and res.error is not None


@respx.mock
async def test_rate_limit_surfaces_as_error_not_absent():
    # 429 must be reported (retry next run), never read as "not found" — reliability contract.
    respx.get(url__startswith=_ENDPOINT).mock(return_value=httpx.Response(429, json={}))
    a = DblpAdapter(client=httpx.AsyncClient())
    res = await a.search_by_metadata(_entry())
    await a.aclose()
    assert res.error is not None and res.records == []


@respx.mock
async def test_unexpected_json_shape_is_an_error():
    respx.get(url__startswith=_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"exception": "query timed out"})
    )
    a = DblpAdapter(client=httpx.AsyncClient())
    res = await a.search_by_metadata(_entry())
    await a.aclose()
    assert res.records == [] and "unexpected SPARQL response" in res.error


@respx.mock
async def test_title_without_searchable_words_is_an_error_without_a_request():
    route = respx.get(url__startswith=_ENDPOINT)
    a = DblpAdapter(client=httpx.AsyncClient())
    res = await a.search_by_metadata(_entry("$\\mathcal{X}$ — 学习"))
    await a.aclose()
    assert res.records == [] and "no searchable word" in res.error
    assert route.call_count == 0


# ── end-to-end: a URL-only conference paper verifies deterministically (no LLM) ─


@respx.mock
async def test_url_only_inproceedings_verified_via_dblp(tmp_path):
    """pmlr-v202-santurkar23a regression: cited only by its mlr.press proceedings URL (no DOI).
    Crossref returns nothing; DBLP returns the exact record, and because a bare URL is not a scoring
    anchor the entry takes the strict backfill path and auto-accepts — no LLM needed."""
    from reference_audit.cache.store import AuditCache
    from reference_audit.config import AuditConfig
    from reference_audit.pipeline import AuditPipeline
    from reference_audit.sources.crossref import CrossrefAdapter

    respx.get(url__regex=r"api\.crossref\.org/works\?").mock(
        return_value=httpx.Response(200, json={"message": {"items": []}})
    )
    respx.get(url__startswith=_ENDPOINT).mock(side_effect=_recorded_endpoint)

    bib = (
        "@inproceedings{pmlr-v202-santurkar23a,\n"
        "  title={Whose Opinions Do Language Models Reflect?},\n"
        "  author={Santurkar, Shibani and Durmus, Esin and Ladhak, Faisal and Lee, Cinoo and "
        "Liang, Percy and Hashimoto, Tatsunori},\n"
        "  booktitle={Proceedings of the 40th International Conference on Machine Learning},\n"
        "  year={2023},\n"
        "  url={https://proceedings.mlr.press/v202/santurkar23a.html}}\n"
    )
    p = tmp_path / "r.bib"
    p.write_text(bib, encoding="utf-8")

    cache = AuditCache(tmp_path / "c.db", model="test")
    pipe = AuditPipeline(
        AuditConfig(model="test", use_llm=False),
        cache=cache,
        adapters=[CrossrefAdapter(client=httpx.AsyncClient()), DblpAdapter(client=httpx.AsyncClient())],
    )
    report = await pipe.run(None, p)
    await pipe.aclose()
    cache.close()

    v = report.entries[0].verdict
    assert v is not None and v.kind == "exactly_one"
    assert v.artifacts[0].best_record.source == "dblp"
