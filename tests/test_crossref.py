"""Crossref adapter with mocked HTTP (respx). error ≠ not-found is the key invariant."""

import httpx
import respx

from reference_audit.models import Identifiers
from reference_audit.sources.crossref import CrossrefAdapter

CROSSREF_MSG = {
    "message": {
        "DOI": "10.1073/pnas.2120037119",
        "title": ["Toward a theory of evolution as multilevel learning"],
        "author": [
            {"given": "Vitaly", "family": "Vanchurin"},
            {"given": "Yuri I.", "family": "Wolf"},
        ],
        "container-title": ["Proceedings of the National Academy of Sciences"],
        "issued": {"date-parts": [[2022]]},
        "page": "e2120037119",
        "type": "journal-article",
        "is-referenced-by-count": 42,
    }
}


@respx.mock
async def test_lookup_by_id_returns_normalized_record():
    route = respx.get("https://api.crossref.org/works/10.1073/pnas.2120037119").mock(
        return_value=httpx.Response(200, json=CROSSREF_MSG)
    )
    adapter = CrossrefAdapter(client=httpx.AsyncClient())
    res = await adapter.lookup_by_id(Identifiers(doi="10.1073/pnas.2120037119"))
    await adapter.aclose()

    assert route.called
    assert res.error is None
    assert len(res.records) == 1
    rec = res.records[0]
    assert rec.ids.doi == "10.1073/pnas.2120037119"
    assert rec.year == 2022
    assert rec.citation_count == 42
    assert any("Vanchurin" in a for a in rec.authors)


@respx.mock
async def test_book_chapter_captures_all_isbns():
    # A book chapter's Crossref record lists the containing volume's print + electronic ISBNs. The
    # normalizer must keep the whole set (not just the first) so a cite giving either ISBN matches.
    msg = {
        "message": {
            "DOI": "10.1007/978-3-642-60616-8_1",
            "title": ["Nile Floods and Political Disorder in Early Egypt"],
            "author": [{"given": "Fekri A.", "family": "Hassan"}],
            "issued": {"date-parts": [[1997]]},
            "type": "book-chapter",
            "ISBN": ["9783642644764", "9783642606168"],
        }
    }
    respx.get("https://api.crossref.org/works/10.1007/978-3-642-60616-8_1").mock(
        return_value=httpx.Response(200, json=msg)
    )
    adapter = CrossrefAdapter(client=httpx.AsyncClient())
    res = await adapter.lookup_by_id(Identifiers(doi="10.1007/978-3-642-60616-8_1"))
    await adapter.aclose()

    rec = res.records[0]
    assert rec.ids.isbn13 == "9783642644764"  # canonical = first
    assert rec.ids.all_isbn13() == frozenset({"9783642644764", "9783642606168"})


@respx.mock
async def test_404_is_absent_not_error():
    respx.get("https://api.crossref.org/works/10.9999/nope").mock(
        return_value=httpx.Response(404)
    )
    adapter = CrossrefAdapter(client=httpx.AsyncClient())
    res = await adapter.lookup_by_id(Identifiers(doi="10.9999/nope"))
    await adapter.aclose()
    assert res.records == []
    assert res.error is None  # genuinely not found, NOT an error


@respx.mock
async def test_5xx_is_error_not_absent():
    respx.get("https://api.crossref.org/works/10.1/x").mock(return_value=httpx.Response(503))
    adapter = CrossrefAdapter(client=httpx.AsyncClient())
    res = await adapter.lookup_by_id(Identifiers(doi="10.1/x"))
    await adapter.aclose()
    assert res.records == []
    assert res.error is not None  # outage must NOT look like a hallucination


@respx.mock
async def test_relation_becomes_version_link():
    msg = {
        "message": {
            "DOI": "10.1177/03010066251384492",
            "title": ["Multiscale structural complexity as a quantitative measure of visual complexity"],
            "author": [{"given": "Anna", "family": "Kravchenko"}],
            "issued": {"date-parts": [[2026]]},
            "type": "journal-article",
            "relation": {"has-preprint": [{"id": "10.48550/arxiv.2408.04076", "id-type": "doi"}]},
        }
    }
    respx.get("https://api.crossref.org/works/10.1177/03010066251384492").mock(
        return_value=httpx.Response(200, json=msg)
    )
    adapter = CrossrefAdapter(client=httpx.AsyncClient())
    res = await adapter.lookup_by_id(Identifiers(doi="10.1177/03010066251384492"))
    await adapter.aclose()
    rec = res.records[0]
    assert "10.48550/arxiv.2408.04076" in rec.version_links
    assert rec.ids.arxiv_id == "2408.04076"  # recovered from the preprint relation
