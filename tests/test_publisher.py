"""Publisher-of-record resolver: BibTeX export parsing, URL derivation, graceful degradation."""

import httpx
import respx

from reference_audit.config import AuditConfig
from reference_audit.models import Identifiers
from reference_audit.pipeline import AuditPipeline
from reference_audit.sources.crossref import CrossrefAdapter
from reference_audit.sources.http import TransientHTTPError
from reference_audit.sources.normalize import publisher_bibtex_to_record
from reference_audit.sources.publisher import (
    PublisherAdapter,
    _atypon_export_url,
    _silverchair_export_url,
)


def _fake_export(result):
    """Build an injectable `export_fetch` for PublisherAdapter that returns a fixed (status, text),
    or raises if `result` is an exception. Replaces respx mocking of the citation-export endpoint,
    which now goes through the browser-impersonating curl_cffi client (libcurl, invisible to respx).
    The DOI/landing redirects still flow through httpx and stay mocked via respx.
    """

    async def fetch(_url):
        if isinstance(result, BaseException):
            raise result
        return result

    return fetch

# A trimmed Silverchair @proceedings export (MIT Press "Cite" → BibTeX). Note: pages is a single
# article number, and `volume` holds the proceedings *title*, not a number.
_EXPORT = """
@proceedings{10.1162/isal_a_00651,
    author = {Plantec, Erwan and Chan, Bert Wang-Chak},
    title = {Flow-Lenia: Towards open-ended evolution in cellular automata},
    volume = {ALIFE 2023: Ghost in the Machine: Proceedings of the 2023 Artificial Life Conference},
    series = {ALIFE 2022: The 2022 Conference on Artificial Life},
    pages = {131},
    year = {2023},
    doi = {10.1162/isal_a_00651},
}
"""

_CHALLENGE = '<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>'
_LANDING = "https://direct.mit.edu/isal/proceedings/isal2023/35/131/116921"
_EXPORT_URL = (
    "https://direct.mit.edu/Citation/Download"
    "?resourceId=116921&resourceType=3&citationFormat=2"
)

# A trimmed Sage/Atypon @article export (journals.sagepub.com "Cite" → BibTeX). Note: the publisher
# of record states the version year 2026 (vol 55, issue 2) where the aggregators carry 2025 (the
# online-first year) — the exact discrepancy the publisher source exists to settle.
_SAGE_EXPORT = """
@article{doi:10.1177/03010066251384492,
author = {Anna Kravchenko and Andrey A Bagrov and Mikhail I Katsnelson and Veronica Dudarev},
title = {Multiscale structural complexity as a quantitative measure of visual complexity},
journal = {Perception},
volume = {55},
number = {2},
pages = {139-158},
year = {2026},
doi = {10.1177/03010066251384492},
URL = {https://doi.org/10.1177/03010066251384492},
}
"""
_SAGE_LANDING = "https://journals.sagepub.com/doi/10.1177/03010066251384492"
_SAGE_EXPORT_URL = (
    "https://journals.sagepub.com/action/downloadCitation"
    "?doi=10.1177%2F03010066251384492&format=bibtex&include=cit"
)


# ── normalizer (no network) ──────────────────────────────────────────────────


def test_publisher_bibtex_parses_pages_and_drops_title_volume():
    rec = publisher_bibtex_to_record(_EXPORT)
    assert rec is not None
    assert rec.pages == "131"             # the datum no API carried
    assert rec.volume == ""               # proceedings-title 'volume' is not a numeric volume
    assert rec.ids.doi == "10.1162/isal_a_00651"
    assert rec.year == 2023
    assert rec.raw["merged_from"] == ["publisher"]


def test_publisher_bibtex_returns_none_on_challenge_html():
    assert publisher_bibtex_to_record(_CHALLENGE) is None


def test_silverchair_export_url_derivation():
    assert _silverchair_export_url(_LANDING) == _EXPORT_URL
    assert _silverchair_export_url("https://academic.oup.com/x/1") is None  # not Silverchair
    assert _silverchair_export_url("https://direct.mit.edu/isal/article/foo") is None  # non-numeric


def test_sage_atypon_bibtex_parses_year_volume_and_pages():
    rec = publisher_bibtex_to_record(_SAGE_EXPORT)
    assert rec is not None
    assert rec.year == 2026               # version-of-record year, not the aggregators' 2025
    assert rec.volume == "55"
    assert rec.issue == "2"
    assert rec.pages == "139-158"
    assert rec.venue == "Perception"
    assert rec.ids.doi == "10.1177/03010066251384492"
    assert rec.raw["merged_from"] == ["publisher"]


def test_atypon_export_url_derivation():
    assert _atypon_export_url(_SAGE_LANDING, "10.1177/03010066251384492") == _SAGE_EXPORT_URL
    assert _atypon_export_url("https://direct.mit.edu/x/1", "10.1/x") is None  # not Atypon


# ── adapter (mocked network) ─────────────────────────────────────────────────


async def test_publisher_no_doi_is_empty_not_error():
    res = await PublisherAdapter().lookup_by_id(Identifiers())
    assert res.records == [] and res.error is None


@respx.mock
async def test_publisher_fetches_and_parses_export():
    respx.get("https://doi.org/10.1162/isal_a_00651").mock(
        return_value=httpx.Response(302, headers={"Location": _LANDING})
    )
    respx.get(_LANDING).mock(return_value=httpx.Response(403, text=_CHALLENGE))  # body bot-walled
    ad = PublisherAdapter(export_fetch=_fake_export((200, _EXPORT)))
    res = await ad.lookup_by_id(Identifiers(doi="10.1162/isal_a_00651"))
    await ad.aclose()
    assert res.error is None
    assert len(res.records) == 1
    assert res.records[0].pages == "131"
    assert res.records[0].source == "publisher"


@respx.mock
async def test_publisher_bot_walled_export_is_reported_error_not_absent():
    # The export endpoint itself is Cloudflare-walled (403). Reliability: surface an *error* (a
    # human-retrievable datum we could not auto-fetch), never an empty 'not found'.
    respx.get("https://doi.org/10.1/x").mock(
        return_value=httpx.Response(302, headers={"Location": _LANDING})
    )
    respx.get(_LANDING).mock(return_value=httpx.Response(403, text=_CHALLENGE))
    # Export endpoint itself is bot-walled (403) → the impersonating fetcher raises TransientHTTPError.
    ad = PublisherAdapter(export_fetch=_fake_export(TransientHTTPError("http 403")))
    res = await ad.lookup_by_id(Identifiers(doi="10.1/x"))
    await ad.aclose()
    assert res.records == []
    assert res.error is not None and "not retrievable" in res.error


@respx.mock
async def test_publisher_fetches_sage_atypon_export():
    # DOI → Sage landing → Atypon citation export (BibTeX). The publisher-of-record year (2026)
    # is what the field check then trusts over the aggregators' 2025.
    respx.get("https://doi.org/10.1177/03010066251384492").mock(
        return_value=httpx.Response(302, headers={"Location": _SAGE_LANDING})
    )
    respx.get(_SAGE_LANDING).mock(return_value=httpx.Response(200, text="<html>landing</html>"))
    ad = PublisherAdapter(export_fetch=_fake_export((200, _SAGE_EXPORT)))
    res = await ad.lookup_by_id(Identifiers(doi="10.1177/03010066251384492"))
    await ad.aclose()
    assert res.error is None
    assert len(res.records) == 1
    assert res.records[0].year == 2026
    assert res.records[0].source == "publisher"


@respx.mock
async def test_publisher_sage_cloudflare_challenge_is_reported_not_absent():
    # The real production case: the Atypon export is Cloudflare-challenged (HTTP 200 with a
    # JS-challenge body, not BibTeX). Reliability: surface an error pointing at the export URL so a
    # human can retrieve it — never a silent 'absent', never a guessed year.
    respx.get("https://doi.org/10.1177/03010066251384492").mock(
        return_value=httpx.Response(302, headers={"Location": _SAGE_LANDING})
    )
    respx.get(_SAGE_LANDING).mock(return_value=httpx.Response(200, text="<html>landing</html>"))
    # Impersonation passes the TLS fingerprint but the platform serves an escalated JS challenge
    # body (HTTP 200, not BibTeX) → reported error, never a silent 'absent' or guessed year.
    ad = PublisherAdapter(export_fetch=_fake_export((200, _CHALLENGE)))
    res = await ad.lookup_by_id(Identifiers(doi="10.1177/03010066251384492"))
    await ad.aclose()
    assert res.records == []
    assert res.error is not None and "not a citation export" in res.error


@respx.mock
async def test_publisher_unknown_platform_is_empty_not_error():
    respx.get("https://doi.org/10.1/y").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/article/1"})
    )
    respx.get("https://example.com/article/1").mock(return_value=httpx.Response(200, text="x"))
    ad = PublisherAdapter()
    res = await ad.lookup_by_id(Identifiers(doi="10.1/y"))
    await ad.aclose()
    assert res.records == [] and res.error is None


# ── pipeline integration: discovered-DOI enrichment surfaces the real page error ──────

_DOILESS_BIB = (
    "@inproceedings{plantec, "
    "title={Flow Lenia Open Ended Evolution In Cellular Automata}, "
    "author={Plantec, Erwan and Chan, Bert}, pages={131--144}, year={2023}}"
)
_CR_ITEM = {
    "DOI": "10.1162/isal_a_00651",
    "title": ["Flow Lenia Open Ended Evolution In Cellular Automata"],
    "author": [{"given": "Erwan", "family": "Plantec"}, {"given": "Bert", "family": "Chan"}],
    "container-title": ["The 2023 Conference on Artificial Life"],
    "issued": {"date-parts": [[2023]]},
    "type": "proceedings-article",  # NOTE: no `page` — only the publisher export has it
}


@respx.mock
async def test_enrichment_uses_backfilled_doi_to_flag_fabricated_pages(tmp_path):
    # Metadata search backfills the DOI (no pages); enrichment then fetches the publisher export by
    # that DOI and the field check flags the entry's '131--144' against the canonical article '131'.
    respx.get(url__regex=r"api\.crossref\.org/works\?").mock(
        return_value=httpx.Response(200, json={"message": {"items": [_CR_ITEM]}})
    )
    respx.get(url__regex=r"api\.crossref\.org/works/10\.1162").mock(
        return_value=httpx.Response(200, json={"message": _CR_ITEM})
    )
    respx.get("https://doi.org/10.1162/isal_a_00651").mock(
        return_value=httpx.Response(302, headers={"Location": _LANDING})
    )
    respx.get(_LANDING).mock(return_value=httpx.Response(403, text=_CHALLENGE))
    bib = tmp_path / "r.bib"
    bib.write_text(_DOILESS_BIB, encoding="utf-8")
    pipe = AuditPipeline(
        AuditConfig(model="t"),
        adapters=[CrossrefAdapter(), PublisherAdapter(export_fetch=_fake_export((200, _EXPORT)))],
        llm=None,
    )
    report = await pipe.run(None, bib)
    await pipe.aclose()

    audit = report.entries[0]
    assert audit.verdict.kind == "exactly_one"
    assert any("DOI found via crossref: 10.1162/isal_a_00651" in i for i in audit.issues)
    by_field = {f.field: f for f in audit.field_findings}
    assert by_field["pages"].status == "error"          # the previously-hidden bug, now caught
    assert "publisher" in by_field["pages"].sources      # sourced from the authority of record
    assert "article number" in by_field["pages"].detail
