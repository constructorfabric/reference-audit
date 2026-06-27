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
