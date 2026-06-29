"""Google Books: volume-id extraction, normalization, and the adapter (mocked HTTP).

Regression anchor: acemoglu2012why ("Why Nations Fail: The Origins of Power, Prosperity, and
Poverty") — Open Library's strict title search + an off-by-one ISBN both miss it, but the .bib
carries a Google Books volume id, which resolves the exact volume with no fuzzy matching.
"""

from __future__ import annotations

import httpx
import respx

from reference_audit.models import BibEntry, EntryType, Identifiers
from reference_audit.parsing.bib import _identifiers_from_fields
from reference_audit.parsing.identifiers import normalize_google_books_id
from reference_audit.sources.google_books import GoogleBooksAdapter
from reference_audit.sources.normalize import google_books_volume_to_record

# Trimmed shape of GET /volumes/yIV_NMDDIvYC.
_VOLUME = {
    "id": "yIV_NMDDIvYC",
    "volumeInfo": {
        "title": "Why Nations Fail",
        "subtitle": "The Origins of Power, Prosperity, and Poverty",
        "authors": ["Daron Acemoglu", "James A. Robinson"],
        "publisher": "Crown",
        "publishedDate": "2012-03-20",
        "industryIdentifiers": [
            {"type": "ISBN_13", "identifier": "9780307719232"},
            {"type": "ISBN_10", "identifier": "0307719227"},
        ],
    },
}


# ── volume-id extraction ──────────────────────────────────────────────────────


def test_extract_volume_id_from_url():
    assert (
        normalize_google_books_id("https://books.google.com.sg/books?id=yIV_NMDDIvYC")
        == "yIV_NMDDIvYC"
    )
    # other params around it
    assert (
        normalize_google_books_id("https://books.google.com/books?hl=en&id=AbC-1_2&pg=PA3")
        == "AbC-1_2"
    )


def test_extract_volume_id_ignores_non_google_urls():
    assert normalize_google_books_id("https://example.org/x?id=yIV_NMDDIvYC") is None
    assert normalize_google_books_id("https://doi.org/10.1/2") is None
    assert normalize_google_books_id(None) is None


def test_bib_fields_populate_google_books_id():
    ids = _identifiers_from_fields(
        {"url": "https://books.google.com.sg/books?id=yIV_NMDDIvYC"}
    )
    assert ids.google_books == "yIV_NMDDIvYC"
    # the URL is preserved (real landing page), unlike an openalex.org Work URL
    assert ids.url == "https://books.google.com.sg/books?id=yIV_NMDDIvYC"


# ── normalization ─────────────────────────────────────────────────────────────


def test_normalize_recombines_title_and_subtitle():
    rec = google_books_volume_to_record(_VOLUME)
    assert rec.title == "Why Nations Fail: The Origins of Power, Prosperity, and Poverty"
    assert rec.year == 2012
    assert rec.publisher == "Crown"
    assert rec.ids.isbn13 == "9780307719232"  # prefers ISBN_13
    assert rec.ids.google_books == "yIV_NMDDIvYC"
    assert rec.source == "google_books"


# ── adapter ───────────────────────────────────────────────────────────────────


@respx.mock
async def test_lookup_by_volume_id():
    route = respx.get(url__regex=r"googleapis\.com/books/v1/volumes/yIV_NMDDIvYC").mock(
        return_value=httpx.Response(200, json=_VOLUME)
    )
    a = GoogleBooksAdapter(client=httpx.AsyncClient())
    res = await a.lookup_by_id(Identifiers(google_books="yIV_NMDDIvYC"))
    await a.aclose()
    assert route.called
    assert res.records[0].ids.google_books == "yIV_NMDDIvYC"
    assert res.error is None


@respx.mock
async def test_metadata_search_includes_subtitle_and_author():
    captured = {}

    def _capture(request):
        captured["q"] = request.url.params.get("q")
        return httpx.Response(200, json={"items": [_VOLUME]})

    respx.get(url__startswith="https://www.googleapis.com/books/v1/volumes").mock(side_effect=_capture)
    a = GoogleBooksAdapter(client=httpx.AsyncClient())
    entry = BibEntry(
        key="k",
        entry_type=EntryType.BOOK,
        title="Why Nations Fail: The Origins of Power, Prosperity, and Poverty",
        authors=["Acemoglu, D.", "Robinson, J.A."],
    )
    res = await a.search_by_metadata(entry)
    await a.aclose()
    assert 'intitle:"Why Nations Fail' in captured["q"]
    assert "inauthor:Acemoglu" in captured["q"]
    assert res.records[0].title.startswith("Why Nations Fail:")


@respx.mock
async def test_cited_volume_id_pins_book_over_samename_journal_review(tmp_path):
    """acemoglu2012why regression: a crossref book *review* (journal-article reusing the book's
    title+authors, carrying a DOI) must NOT be matched and backfilled onto the @book. The cited
    Google Books volume id pins the actual book as the identity instead.
    """
    from reference_audit.cache.store import AuditCache
    from reference_audit.config import AuditConfig
    from reference_audit.pipeline import AuditPipeline
    from reference_audit.sources.crossref import CrossrefAdapter

    review_item = {
        "DOI": "10.1355/ae29-2j",
        "title": ["Why Nations Fail: The Origins of Power, Prosperity and Poverty"],
        "author": [{"family": "Acemoglu"}, {"family": "Robinson"}],
        "container-title": ["ASEAN ECONOMIC BULLETIN"],
        "issued": {"date-parts": [[2012]]},
        "type": "journal-article",
    }
    respx.get(url__regex=r"api\.crossref\.org/works\?").mock(
        return_value=httpx.Response(200, json={"message": {"items": [review_item]}})
    )

    def _gbooks(request):
        # /volumes/{id} and /volumes?q=… both resolve to the real book volume
        return httpx.Response(200, json=_VOLUME if request.url.path.endswith("/yIV_NMDDIvYC")
                              else {"items": [_VOLUME]})

    respx.get(url__startswith="https://www.googleapis.com/books/v1/volumes").mock(side_effect=_gbooks)

    bib = (
        "@book{acemoglu2012why,\n"
        "  title={Why Nations Fail: The Origins of Power, Prosperity, and Poverty},\n"
        "  author={Acemoglu, D. and Robinson, J.A.},\n"
        "  isbn={9780307719232},\n"
        "  url={https://books.google.com.sg/books?id=yIV_NMDDIvYC},\n"
        "  year={2012}, publisher={Crown}}\n"
    )
    p = tmp_path / "r.bib"
    p.write_text(bib, encoding="utf-8")

    cache = AuditCache(tmp_path / "c.db", model="test")
    pipe = AuditPipeline(
        AuditConfig(model="test", use_llm=False),
        cache=cache,
        adapters=[CrossrefAdapter(client=httpx.AsyncClient()), GoogleBooksAdapter(client=httpx.AsyncClient())],
    )
    report = await pipe.run(None, p)
    await pipe.aclose()
    cache.close()

    v = report.entries[0].verdict
    assert v.kind == "exactly_one"
    assert v.artifacts[0].best_record.source == "google_books"
    assert "Google Books" in v.rationale
    # the journal-article review's DOI must NOT be backfilled onto the book
    assert v.artifacts[0].merged_ids.doi is None


@respx.mock
async def test_rate_limit_surfaces_as_error_not_absent():
    # 429 must be reported (retry next run), never read as "not found" — reliability contract.
    respx.get(url__startswith="https://www.googleapis.com/books/v1/volumes").mock(
        return_value=httpx.Response(429, json={"error": "Rate limit"})
    )
    a = GoogleBooksAdapter(client=httpx.AsyncClient())
    res = await a.lookup_by_id(Identifiers(isbn13="9780307719218"))
    await a.aclose()
    assert res.error is not None
    assert res.records == []
