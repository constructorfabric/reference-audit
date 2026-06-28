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
