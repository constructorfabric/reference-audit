"""End-to-end book audit (mocked Crossref + Open Library).

The regression this guards: a 1976 book whose only DOI-bearing match is a 2018 reprint must NOT have
its original-edition year/publisher flagged as wrong. Instead the cited edition is confirmed against
Open Library's actual editions (step 1), and the latest edition is offered as a better version
(step 2).
"""

from __future__ import annotations

import httpx
import respx

from reference_audit.config import AuditConfig
from reference_audit.pipeline import AuditPipeline
from reference_audit.sources.crossref import CrossrefAdapter
from reference_audit.sources.openalex import OpenAlexAdapter
from reference_audit.sources.openlibrary import OpenLibraryAdapter

# DOI-less 1976 book; the only DOI-bearing record the sources return is the 2018 Routledge reprint.
_BOOK_BIB = (
    "@book{mabook, title={Modern Theory Of Critical Phenomena}, author={Ma, Shang-Keng}, "
    "year={1976}, publisher={W. A. Benjamin}, address={Reading, Mass.}}"
)

_CR_REPRINT = {
    "DOI": "10.4324/9780429498886",
    "title": ["Modern Theory of Critical Phenomena"],
    "author": [{"given": "Shang-Keng", "family": "Ma"}],
    "issued": {"date-parts": [[2018]]},
    "publisher": "Routledge",
    "ISBN": ["9780429498886"],
    "type": "book",
}

# Open Library search.json answers both the candidate metadata search and fetch_editions' work-key
# lookup (same endpoint). Two split work records: the 1976 original and the 2018 reprint cluster.
_OL_SEARCH = {"docs": [
    {"key": "/works/OL_OLD", "title": "Modern theory of critical phenomena",
     "author_name": ["Shang-Keng Ma"], "first_publish_year": 1976,
     "publisher": ["W. A. Benjamin, Advanced Book Program"], "isbn": ["0805366709"]},
    {"key": "/works/OL_NEW", "title": "Modern Theory of Critical Phenomena",
     "author_name": ["Shang-Keng Ma"], "first_publish_year": 2018,
     "publisher": ["Taylor & Francis Group"], "isbn": ["9780429498886"]},
]}
_OL_ED_OLD = {"entries": [
    {"key": "/books/OLAM", "title": "Modern theory of critical phenomena",
     "publish_date": "1976", "publishers": ["W. A. Benjamin, Advanced Book Program"],
     "isbn_10": ["0805366709"]},
    {"key": "/books/OLBM", "publish_date": "2000", "publishers": ["Perseus Pub."],
     "isbn_10": ["0738203017"]},
]}
_OL_ED_NEW = {"entries": [
    {"key": "/books/OLCM", "publish_date": "2018", "publishers": ["Taylor & Francis Group"],
     "isbn_13": ["9780429498886"]},
]}


def _mock_endpoints() -> None:
    respx.get(url__regex=r"api\.crossref\.org/works\?").mock(
        return_value=httpx.Response(200, json={"message": {"items": [_CR_REPRINT]}})
    )
    respx.get(url__regex=r"openlibrary\.org/search\.json").mock(
        return_value=httpx.Response(200, json=_OL_SEARCH)
    )
    respx.get(url__regex=r"openlibrary\.org/works/OL_OLD/editions\.json").mock(
        return_value=httpx.Response(200, json=_OL_ED_OLD)
    )
    respx.get(url__regex=r"openlibrary\.org/works/OL_NEW/editions\.json").mock(
        return_value=httpx.Response(200, json=_OL_ED_NEW)
    )


@respx.mock
async def test_book_original_edition_not_flagged_and_latest_suggested(tmp_path):
    _mock_endpoints()
    bib = tmp_path / "r.bib"
    bib.write_text(_BOOK_BIB, encoding="utf-8")
    pipe = AuditPipeline(
        AuditConfig(model="t", use_llm=False),
        adapters=[CrossrefAdapter(client=httpx.AsyncClient()),
                  OpenLibraryAdapter(client=httpx.AsyncClient())],
    )
    report = await pipe.run(None, bib)
    await pipe.aclose()

    audit = report.entries[0]
    assert audit.verdict.kind == "exactly_one"  # the book is matched (to the reprint's work)

    by_field = {f.field: f for f in audit.field_findings}
    # Step 1: year/publisher are checked against the CITED 1976 edition, not the 2018 reprint.
    assert by_field["year"].status == "ok"
    assert by_field["publisher"].status == "formatting"  # 'W. A. Benjamin' ⊆ the fuller imprint

    # The old false positives must be gone.
    assert not any("needs review" in i and "year" in i for i in audit.issues)
    assert not any("looks wrong" in i and "publisher" in i for i in audit.issues)

    # Step 2: the latest edition is offered as a better version.
    assert any("newer edition is available" in i and "2018" in i for i in audit.issues)


# russell2019human: a trade book cited ONLY by an OpenAlex Work URL. Crossref + Open Library do not
# have a clean record for it (the old false-positive NO MATCH), but the cited Work id resolves it.
_OA_BOOK_BIB = (
    "@book{russell2019human, "
    "title={Human Compatible: Artificial Intelligence and the Problem of Control}, "
    "author={Stuart Russell}, year={2019}, publisher={Viking}, "
    "url={https://openalex.org/W3034344071}}"
)
_OA_BOOK_WORK = {
    "id": "https://openalex.org/W3034344071",
    "ids": {"openalex": "https://openalex.org/W3034344071"},
    "title": "Human Compatible: Artificial Intelligence and the Problem of Control",
    "publication_year": 2019,
    "type": "book",
    "authorships": [{"author": {"display_name": "Stuart Russell"}}],
    "cited_by_count": 42,
}


# A similar-titled Crossref CHAPTER (own DOI/ISBN) the title pooler used to merge Russell's book
# into — backfilling the chapter's identifiers onto the book and raising a spurious title review.
_CR_CHAPTER = {
    "DOI": "10.1007/978-3-030-86144-5_3",
    "title": ["Artificial Intelligence and the Problem of Control"],
    "author": [{"given": "Stuart", "family": "Russell"}],
    "issued": {"date-parts": [[2022]]},
    "publisher": "Springer",
    "ISBN": ["9783030861438"],
    "type": "book-chapter",
}


@respx.mock
async def test_book_with_openalex_id_resolves_via_work_lookup(tmp_path):
    # Open Library lacks the trade title and Crossref only offers a similar-titled chapter; the cited
    # OpenAlex Work id is the authoritative key and must win identity (no wrong-id backfill).
    respx.get(url__regex=r"api\.crossref\.org/works\?").mock(
        return_value=httpx.Response(200, json={"message": {"items": [_CR_CHAPTER]}})
    )
    respx.get(url__regex=r"openlibrary\.org/search\.json").mock(
        return_value=httpx.Response(200, json={"docs": []})
    )
    respx.get(url__regex=r"api\.openalex\.org/works/W3034344071").mock(
        return_value=httpx.Response(200, json=_OA_BOOK_WORK)
    )
    bib = tmp_path / "r.bib"
    bib.write_text(_OA_BOOK_BIB, encoding="utf-8")
    pipe = AuditPipeline(
        AuditConfig(model="t", use_llm=False),
        adapters=[CrossrefAdapter(client=httpx.AsyncClient()),
                  OpenLibraryAdapter(client=httpx.AsyncClient()),
                  OpenAlexAdapter(client=httpx.AsyncClient())],
    )
    report = await pipe.run(None, bib)
    await pipe.aclose()

    audit = report.entries[0]
    # The regression: this correct book was flagged NO MATCH. Now the Work id resolves it cleanly.
    assert audit.verdict.kind == "exactly_one"
    best = audit.verdict.artifacts[0].best_record
    assert best.source == "openalex"
    assert best.ids.openalex == "W3034344071"
    # The chapter's identifiers must NOT be attributed to the book (reliability: no wrong guesses).
    assert best.ids.doi is None
    assert not any("10.1007/978-3-030-86144-5_3" in i for i in audit.issues)
    assert not any("9783030861438" in i for i in audit.issues)
    assert not any("title" in i and "needs review" in i for i in audit.issues)


@respx.mock
async def test_cited_openalex_id_with_mismatched_work_is_not_confirmed(tmp_path):
    # Safety gate: if the cited Work id resolves to a DIFFERENT-titled work (a wrong/stale id), the
    # override must NOT pin it — the entry stays unconfirmed rather than being falsely matched.
    wrong_work = dict(_OA_BOOK_WORK, title="An Entirely Unrelated Treatise on Beekeeping",
                      authors=None, authorships=[{"author": {"display_name": "Someone Else"}}])
    respx.get(url__regex=r"api\.crossref\.org/works\?").mock(
        return_value=httpx.Response(200, json={"message": {"items": []}})
    )
    respx.get(url__regex=r"openlibrary\.org/search\.json").mock(
        return_value=httpx.Response(200, json={"docs": []})
    )
    respx.get(url__regex=r"api\.openalex\.org/works/W3034344071").mock(
        return_value=httpx.Response(200, json=wrong_work)
    )
    bib = tmp_path / "r.bib"
    bib.write_text(_OA_BOOK_BIB, encoding="utf-8")
    pipe = AuditPipeline(
        AuditConfig(model="t", use_llm=False),
        adapters=[CrossrefAdapter(client=httpx.AsyncClient()),
                  OpenLibraryAdapter(client=httpx.AsyncClient()),
                  OpenAlexAdapter(client=httpx.AsyncClient())],
    )
    report = await pipe.run(None, bib)
    await pipe.aclose()

    audit = report.entries[0]
    # Not pinned to the mismatched Work (would be a false confirmation).
    assert audit.verdict is None or audit.verdict.rationale != (
        "confirmed via OpenAlex (cited Work id resolved)"
    )


@respx.mock
async def test_book_openlibrary_outage_downgrades_no_match_to_unresolved(tmp_path):
    # Reliability: when Open Library (the book authority of record) is unreachable, the article-
    # centric matcher's 'NO MATCH (possible hallucination)' verdict is NOT trustworthy for a book.
    # We must not assert a hallucination we never actually checked — leave it unresolved (retry next
    # run) instead. Regression for the diamond2011collapse transport-error report.
    book_bib = (
        "@book{diamond2011collapse, "
        "title={Collapse: How Societies Choose to Fail or Succeed}, "
        "author={Diamond, Jared}, year={2011}, publisher={Penguin}, isbn={9780143117001}}"
    )
    # Crossref finds nothing real; Open Library's search.json yields a work key (so editions are
    # attempted) but is an UNRELATED record, so the generic matcher auto-rejects it → verdict 'none'.
    respx.get(url__regex=r"api\.crossref\.org/works\?").mock(
        return_value=httpx.Response(200, json={"message": {"items": []}})
    )
    respx.get(url__regex=r"openlibrary\.org/search\.json").mock(
        return_value=httpx.Response(200, json={"docs": [
            {"key": "/works/OL_UNRELATED", "title": "An Unrelated Treatise on Beekeeping",
             "author_name": ["Someone Else"], "first_publish_year": 1999},
        ]})
    )
    # The editions fetch transport-errors (the real-world "All connection attempts failed").
    respx.get(url__regex=r"openlibrary\.org/works/OL_UNRELATED/editions\.json").mock(
        side_effect=httpx.ConnectError("All connection attempts failed")
    )
    bib = tmp_path / "r.bib"
    bib.write_text(book_bib, encoding="utf-8")
    pipe = AuditPipeline(
        AuditConfig(model="t", use_llm=False),
        adapters=[CrossrefAdapter(client=httpx.AsyncClient()),
                  OpenLibraryAdapter(client=httpx.AsyncClient())],
    )
    report = await pipe.run(None, bib)
    await pipe.aclose()

    audit = report.entries[0]
    # NOT a hallucination: the authority was unreachable, so the entry is left unresolved (no verdict).
    assert audit.verdict is None
    # ...and the outage is reported (will retry next run), never silently passed.
    assert any(
        "could not verify the cited edition" in i and "will retry next run" in i
        for i in audit.issues
    )


@respx.mock
async def test_book_not_in_openlibrary_raises_gap(tmp_path):
    # Open Library has no record of the book → the cited edition cannot be confirmed, so the gap is
    # reported to the user rather than silently passing the (reprint-matched) book.
    respx.get(url__regex=r"api\.crossref\.org/works\?").mock(
        return_value=httpx.Response(200, json={"message": {"items": [_CR_REPRINT]}})
    )
    respx.get(url__regex=r"openlibrary\.org/search\.json").mock(
        return_value=httpx.Response(200, json={"docs": []})
    )
    bib = tmp_path / "r.bib"
    bib.write_text(_BOOK_BIB, encoding="utf-8")
    pipe = AuditPipeline(
        AuditConfig(model="t", use_llm=False),
        adapters=[CrossrefAdapter(client=httpx.AsyncClient()),
                  OpenLibraryAdapter(client=httpx.AsyncClient())],
    )
    report = await pipe.run(None, bib)
    await pipe.aclose()

    audit = report.entries[0]
    assert audit.verdict.kind == "exactly_one"  # identity unaffected; only verification reports a gap
    assert any("could not verify the cited edition" in i for i in audit.issues)
    # ...and no edition-grounded year/publisher finding was fabricated.
    assert not any(f.field in ("year", "publisher") for f in audit.field_findings)


# amsden1989asia: a real @book cited by a CHAPTER-level Oxford DOI. The DOI resolves to a chapter
# ("A History of Backwardness") whose title differs from the book, so the generic matcher rejects it
# — but the chapter record carries the book's ISBNs, which resolve the book in Open Library.
_CHAPTER_DOI = {
    "DOI": "10.1093/abc123.003.0002",
    "title": ["A History of Backwardness"],
    "container-title": ["Asia's Next Giant"],
    "author": [{"given": "Alice H.", "family": "Amsden"}],
    "issued": {"date-parts": [[1992]]},
    "publisher": "Oxford University Press",
    # Print ISBN (OL indexes it) + an online ISBN OL does NOT index — exercises "try every ISBN".
    "ISBN": ["9780195076035", "9780199870691"],
    "type": "book-chapter",
}
_CHAPTER_BOOK_BIB = (
    "@book{amsden1989asia, title={Asia's Next Giant: South Korea and Late Industrialization}, "
    "author={Amsden, Alice H}, year={1989}, publisher={Oxford University Press}, "
    "doi={10.1093/abc123.003.0002}}"
)


@respx.mock
async def test_book_cited_by_chapter_doi_confirmed_via_isbn_backfill(tmp_path):
    # by-id lookup and metadata search both return the chapter (title ≠ book title → generic NO MATCH).
    respx.get(url__regex=r"api\.crossref\.org/works/10\.1093").mock(
        return_value=httpx.Response(200, json={"message": _CHAPTER_DOI})
    )
    respx.get(url__regex=r"api\.crossref\.org/works\?").mock(
        return_value=httpx.Response(200, json={"message": {"items": [_CHAPTER_DOI]}})
    )
    # Open Library indexes the book by its print ISBN only; the online ISBN and the subtitle-bearing
    # title search both miss (the route order makes the specific ISBN win over the empty catch-all).
    respx.get(url__regex=r"openlibrary\.org/search\.json\?.*isbn=9780195076035").mock(
        return_value=httpx.Response(200, json={"docs": [
            {"key": "/works/OLW", "title": "Asia's next giant"}]})
    )
    respx.get(url__regex=r"openlibrary\.org/search\.json").mock(
        return_value=httpx.Response(200, json={"docs": []})
    )
    respx.get(url__regex=r"openlibrary\.org/works/OLW/editions\.json").mock(
        return_value=httpx.Response(200, json={"entries": [
            {"key": "/books/OL1", "title": "Asia's Next Giant", "publish_date": "1989",
             "publishers": ["Oxford University Press"], "isbn_13": ["9780195058529"]},
            {"key": "/books/OL2", "title": "Asia's Next Giant", "publish_date": "1992",
             "publishers": ["Oxford University Press"], "isbn_13": ["9780195076035"]},
        ]})
    )
    bib = tmp_path / "r.bib"
    bib.write_text(_CHAPTER_BOOK_BIB, encoding="utf-8")
    pipe = AuditPipeline(
        AuditConfig(model="t", use_llm=False),
        adapters=[CrossrefAdapter(client=httpx.AsyncClient()),
                  OpenLibraryAdapter(client=httpx.AsyncClient())],
    )
    report = await pipe.run(None, bib)
    await pipe.aclose()

    audit = report.entries[0]
    # The regression: this real book was flagged NO MATCH. The chapter DOI's ISBNs now backfill into
    # Open Library, which confirms the book — grounded on the CITED 1989 edition, not the 1992 reprint.
    assert audit.verdict.kind == "exactly_one"
    best = audit.verdict.artifacts[0].best_record
    assert best.source == "openlibrary"
    assert best.year == 1989
    assert any("chapter/component DOI" in i for i in audit.issues)
