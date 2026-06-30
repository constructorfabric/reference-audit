"""DBLP: hit normalization + the adapter + an end-to-end verify (mocked HTTP).

Regression anchors: the premier ML venues mint no DOI and are cited only by a proceedings URL —
`pmlr-v202-santurkar23a` ("Whose Opinions Do Language Models Reflect?", ICML/PMLR), with siblings at
NeurIPS and ICLR. The article-centric aggregators cover them thinly; DBLP indexes them exactly, so a
URL-only @inproceedings reaches a deterministic verdict.
"""

from __future__ import annotations

import httpx
import respx

from reference_audit.models import BibEntry, EntryType
from reference_audit.sources.dblp import DblpAdapter
from reference_audit.sources.normalize import dblp_hit_to_record

# Trimmed shape of GET /search/publ/api?q=…&format=json — a real ICML/PMLR hit.
_HIT = {
    "info": {
        "authors": {
            "author": [
                {"@pid": "153/2146", "text": "Shibani Santurkar"},
                {"@pid": "219/6227", "text": "Esin Durmus"},
                {"@pid": "194/1214", "text": "Faisal Ladhak"},
                {"@pid": "344/3500", "text": "Cinoo Lee"},
                {"@pid": "04/1701", "text": "Percy Liang"},
                {"@pid": "66/7232", "text": "Tatsunori Hashimoto"},
            ]
        },
        "title": "Whose Opinions Do Language Models Reflect?",
        "venue": "ICML",
        "pages": "29971-30004",
        "year": "2023",
        "type": "Conference and Workshop Papers",
        "key": "conf/icml/SanturkarDLLLH23",
        "ee": "https://proceedings.mlr.press/v202/santurkar23a.html",
        "url": "https://dblp.org/rec/conf/icml/SanturkarDLLLH23",
    }
}


# ── normalization ─────────────────────────────────────────────────────────────


def test_normalize_keeps_proceedings_url_and_strips_title_period():
    rec = dblp_hit_to_record(_HIT)
    assert rec.source == "dblp"
    assert rec.title == "Whose Opinions Do Language Models Reflect?"
    assert rec.year == 2023
    assert rec.venue == "ICML"
    assert rec.pages == "29971-30004"
    assert len(rec.authors) == 6 and rec.authors[0] == "Shibani Santurkar"
    # the `ee` proceedings page (the very URL the .bib cites) is kept as the record URL
    assert rec.ids.url == "https://proceedings.mlr.press/v202/santurkar23a.html"
    assert rec.ids.doi is None
    assert rec.is_preprint is False


def test_normalize_single_author_object_and_homonym_number():
    # DBLP collapses a 1-element array to a bare object, and appends a 4-digit homonym number.
    hit = {"info": {"authors": {"author": {"text": "Bowen Baker 0001"}}, "title": "X.", "year": "2024"}}
    rec = dblp_hit_to_record(hit)
    assert rec.authors == ["Bowen Baker"]
    assert rec.title == "X"


def test_normalize_informal_publication_is_preprint_with_arxiv():
    hit = {
        "info": {
            "title": "Some Preprint",
            "year": "2024",
            "type": "Informal and Other Publications",
            "ee": "https://arxiv.org/abs/2406.04235",
        }
    }
    rec = dblp_hit_to_record(hit)
    assert rec.is_preprint is True
    assert rec.ids.arxiv_id == "2406.04235"


def test_normalize_venue_list_takes_first():
    hit = {"info": {"title": "T", "venue": ["NeurIPS", "CoRR"], "year": "2025"}}
    assert dblp_hit_to_record(hit).venue == "NeurIPS"


# ── adapter ───────────────────────────────────────────────────────────────────


@respx.mock
async def test_search_by_metadata():
    captured = {}

    def _capture(request):
        captured["q"] = request.url.params.get("q")
        captured["format"] = request.url.params.get("format")
        return httpx.Response(200, json={"result": {"hits": {"hit": [_HIT]}}})

    respx.get(url__startswith="https://dblp.org/search/publ/api").mock(side_effect=_capture)
    a = DblpAdapter(client=httpx.AsyncClient())
    entry = BibEntry(
        key="k",
        entry_type=EntryType.INPROCEEDINGS,
        title="Whose Opinions Do Language Models Reflect?",
    )
    res = await a.search_by_metadata(entry)
    await a.aclose()
    assert captured["q"] == "Whose Opinions Do Language Models Reflect?"
    assert captured["format"] == "json"
    assert res.error is None
    assert res.records[0].source_native_id == "conf/icml/SanturkarDLLLH23"


@respx.mock
async def test_single_hit_object_handled():
    # one result → DBLP returns `hit` as a bare object, not a list
    respx.get(url__startswith="https://dblp.org/search/publ/api").mock(
        return_value=httpx.Response(200, json={"result": {"hits": {"hit": _HIT}}})
    )
    a = DblpAdapter(client=httpx.AsyncClient())
    res = await a.search_by_metadata(
        BibEntry(key="k", entry_type=EntryType.INPROCEEDINGS, title="Whose Opinions…")
    )
    await a.aclose()
    assert len(res.records) == 1


@respx.mock
async def test_no_hits_is_empty_not_error():
    respx.get(url__startswith="https://dblp.org/search/publ/api").mock(
        return_value=httpx.Response(200, json={"result": {"hits": {"@total": "0"}}})
    )
    a = DblpAdapter(client=httpx.AsyncClient())
    res = await a.search_by_metadata(
        BibEntry(key="k", entry_type=EntryType.INPROCEEDINGS, title="Nonexistent")
    )
    await a.aclose()
    assert res.records == [] and res.error is None


@respx.mock
async def test_rate_limit_surfaces_as_error_not_absent():
    # 429 must be reported (retry next run), never read as "not found" — reliability contract.
    respx.get(url__startswith="https://dblp.org/search/publ/api").mock(
        return_value=httpx.Response(429, json={})
    )
    a = DblpAdapter(client=httpx.AsyncClient())
    res = await a.search_by_metadata(
        BibEntry(key="k", entry_type=EntryType.INPROCEEDINGS, title="Whose Opinions…")
    )
    await a.aclose()
    assert res.error is not None and res.records == []


# ── end-to-end: a URL-only conference paper verifies deterministically (no LLM) ─


@respx.mock
async def test_url_only_inproceedings_verified_via_dblp(tmp_path):
    """pmlr-v202-santurkar23a regression: cited only by its mlr.press proceedings URL (no DOI).
    Crossref/OpenAlex/S2 return nothing; DBLP returns the exact record, and because a bare URL is not
    a scoring anchor the entry takes the strict backfill path and auto-accepts — no LLM needed."""
    from reference_audit.cache.store import AuditCache
    from reference_audit.config import AuditConfig
    from reference_audit.pipeline import AuditPipeline
    from reference_audit.sources.crossref import CrossrefAdapter

    respx.get(url__regex=r"api\.crossref\.org/works\?").mock(
        return_value=httpx.Response(200, json={"message": {"items": []}})
    )
    respx.get(url__startswith="https://dblp.org/search/publ/api").mock(
        return_value=httpx.Response(200, json={"result": {"hits": {"hit": [_HIT]}}})
    )

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
