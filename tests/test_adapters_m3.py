"""M3 adapters with mocked HTTP: OpenAlex (version graph), S2, Open Library."""

import httpx
import respx

from reference_audit.models import BibEntry, EntryType, Identifiers
from reference_audit.sources.openalex import OpenAlexAdapter
from reference_audit.sources.openlibrary import OpenLibraryAdapter
from reference_audit.sources.semantic_scholar import SemanticScholarAdapter

OA_WORK = {
    "id": "https://openalex.org/W123",
    "doi": "https://doi.org/10.1177/03010066251384492",
    "ids": {"openalex": "https://openalex.org/W123", "doi": "https://doi.org/10.1177/03010066251384492"},
    "title": "Multiscale structural complexity as a quantitative measure of visual complexity",
    "publication_year": 2026,
    "type": "article",
    "authorships": [{"author": {"display_name": "Anna Kravchenko"}}],
    "primary_location": {"source": {"display_name": "Perception", "type": "journal"}},
    "locations": [
        {"landing_page_url": "https://doi.org/10.1177/03010066251384492",
         "source": {"display_name": "Perception", "type": "journal"}},
        {"landing_page_url": "https://arxiv.org/abs/2408.04076",
         "source": {"display_name": "arXiv", "type": "repository"}},
    ],
    "cited_by_count": 3,
}


@respx.mock
async def test_openalex_captures_version_graph():
    respx.get(url__regex=r"api\.openalex\.org/works/doi:").mock(
        return_value=httpx.Response(200, json=OA_WORK)
    )
    a = OpenAlexAdapter(client=httpx.AsyncClient())
    res = await a.lookup_by_id(Identifiers(doi="10.1177/03010066251384492"))
    await a.aclose()
    rec = res.records[0]
    assert rec.ids.doi == "10.1177/03010066251384492"
    assert rec.openalex_work_id == "https://openalex.org/W123"
    # the published Work also lists the arXiv preprint location → version graph + recovered arxiv id
    assert any("arxiv.org/abs/2408.04076" in link for link in rec.version_links)
    assert rec.ids.arxiv_id == "2408.04076"


@respx.mock
async def test_openalex_lookup_by_work_id():
    # russell2019human: cited only by an openalex.org Work URL → resolve it directly by Work id.
    route = respx.get(url__regex=r"api\.openalex\.org/works/W123").mock(
        return_value=httpx.Response(200, json=OA_WORK)
    )
    a = OpenAlexAdapter(client=httpx.AsyncClient())
    res = await a.lookup_by_id(Identifiers(openalex="W123"))
    await a.aclose()
    assert route.called
    rec = res.records[0]
    assert rec.ids.openalex == "W123"  # bare + matchable by id_agreement


@respx.mock
async def test_semantic_scholar_metadata():
    payload = {
        "data": [
            {
                "paperId": "abc",
                "title": "DreamSim: Learning New Dimensions of Human Visual Similarity",
                "year": 2023,
                "venue": "NeurIPS",
                "authors": [{"name": "Stephanie Fu"}, {"name": "Phillip Isola"}],
                "externalIds": {"DOI": "10.5555/dreamsim", "ArXiv": "2306.09344"},
                "citationCount": 100,
            }
        ]
    }
    respx.get(url__regex=r"api\.semanticscholar\.org/.*/paper/search").mock(
        return_value=httpx.Response(200, json=payload)
    )
    a = SemanticScholarAdapter(client=httpx.AsyncClient())
    entry = BibEntry(key="fu", entry_type=EntryType.INPROCEEDINGS,
                     title="DreamSim: Learning New Dimensions of Human Visual Similarity",
                     authors=["Fu, Stephanie"])
    res = await a.search_by_metadata(entry)
    await a.aclose()
    assert res.records[0].ids.doi == "10.5555/dreamsim"
    assert res.records[0].ids.arxiv_id == "2306.09344"


@respx.mock
async def test_openlibrary_book_backfills_isbn():
    payload = {"docs": [{
        "key": "/works/OL1W", "title": "Fitness Landscapes and the Origin of Species",
        "author_name": ["Sergey Gavrilets"], "first_publish_year": 2004,
        "isbn": ["0691117586", "9780691117584"], "edition_count": 3,
    }]}
    respx.get(url__regex=r"openlibrary\.org/search\.json").mock(
        return_value=httpx.Response(200, json=payload)
    )
    a = OpenLibraryAdapter(client=httpx.AsyncClient())
    entry = BibEntry(key="g", entry_type=EntryType.BOOK,
                     title="Fitness Landscapes and the Origin of Species",
                     authors=["Gavrilets, Sergey"])
    res = await a.search_by_metadata(entry)
    await a.aclose()
    assert res.records[0].ids.isbn13 == "9780691117584"


# ── fetch_editions: per-edition year/publisher/ISBN across split work records ──

_SEARCH_DOCS = {"docs": [
    {"key": "/works/OL_OLD", "title": "Modern theory of critical phenomena"},
    {"key": "/works/OL_NEW", "title": "Modern Theory of Critical Phenomena"},
    {"key": "/works/OL_OTHER", "title": "A Completely Different Book"},  # title gate must drop this
]}
_EDITIONS_OLD = {"entries": [
    {"key": "/books/OLb1M", "title": "Modern theory of critical phenomena",
     "publish_date": "1976", "publishers": ["W. A. Benjamin, Advanced Book Program"],
     "isbn_10": ["0805366709"]},
    {"key": "/books/OLb2M", "title": "Modern theory of critical phenomena",
     "publish_date": "2000", "publishers": ["Perseus Pub."], "isbn_10": ["0738203017"]},
]}
_EDITIONS_NEW = {"entries": [
    {"key": "/books/OLb3M", "title": "Modern Theory of Critical Phenomena",
     "publish_date": "2018", "publishers": ["Taylor & Francis Group"],
     "isbn_13": ["9780429498886"]},
]}


@respx.mock
async def test_openlibrary_fetch_editions_pools_across_works():
    respx.get(url__regex=r"openlibrary\.org/search\.json").mock(
        return_value=httpx.Response(200, json=_SEARCH_DOCS)
    )
    respx.get(url__regex=r"openlibrary\.org/works/OL_OLD/editions\.json").mock(
        return_value=httpx.Response(200, json=_EDITIONS_OLD)
    )
    respx.get(url__regex=r"openlibrary\.org/works/OL_NEW/editions\.json").mock(
        return_value=httpx.Response(200, json=_EDITIONS_NEW)
    )
    # OL_OTHER is dropped by the title gate, so its editions endpoint must never be queried; if it is,
    # respx (default) raises on the unmocked request, failing the test.
    a = OpenLibraryAdapter(client=httpx.AsyncClient())
    entry = BibEntry(key="ma", entry_type=EntryType.BOOK,
                     title="Modern Theory of Critical Phenomena", authors=["Ma, Shang-Keng"])
    res = await a.fetch_editions(entry)
    await a.aclose()
    assert res.query_kind == "editions"
    assert res.error is None
    years = sorted(r.year for r in res.records)
    assert years == [1976, 2000, 2018]
    by_year = {r.year: r for r in res.records}
    assert by_year[1976].publisher == "W. A. Benjamin, Advanced Book Program"
    assert by_year[1976].ids.isbn13 == "9780805366709"  # isbn_10 upgraded to isbn13
    assert by_year[2018].ids.isbn13 == "9780429498886"


@respx.mock
async def test_openlibrary_fetch_editions_surfaces_error_when_nothing_retrieved():
    # A 503 on the work search is an outage, not 'no editions' — must be reported, never cached.
    respx.get(url__regex=r"openlibrary\.org/search\.json").mock(
        return_value=httpx.Response(503, text="busy")
    )
    a = OpenLibraryAdapter(client=httpx.AsyncClient())
    entry = BibEntry(key="ma", entry_type=EntryType.BOOK,
                     title="Modern Theory of Critical Phenomena", authors=["Ma, Shang-Keng"])
    res = await a.fetch_editions(entry)
    await a.aclose()
    assert res.records == []
    assert res.error is not None
