"""Publisher-of-record resolver: BibTeX export parsing, URL derivation, graceful degradation."""

import httpx
import pytest
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
    neurips_export_url,
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


# --- doi_registered: the doi.org Handle API ------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"responseCode": 1, "handle": "10.1109/x"}, True),
        (200, {"responseCode": 200, "handle": "10.1109/x"}, True),   # found, no URL value
        # An unregistered prefix: the resolver answers HTTP 500 for it, the Handle API a 100.
        (404, {"responseCode": 100, "handle": "10.8888/ngtbpa.458568"}, False),
        # A handle prefix the DOI system does not serve: the resolver answers HTTP 400 for it.
        (400, {"responseCode": 301, "message": "That prefix doesn't live here"}, False),
        (500, {"responseCode": 2, "message": "error"}, None),        # a handle-server error
        (429, None, None),                                           # throttled: undetermined
    ],
)
@respx.mock
async def test_doi_registered_reads_the_handle_api_response_code(status, body, expected):
    respx.get(url__startswith="https://doi.org/api/handles/10.").mock(
        return_value=httpx.Response(status, json=body) if body else httpx.Response(status)
    )
    adapter = PublisherAdapter(client=httpx.AsyncClient())
    assert await adapter.doi_registered("10.8888/ngtbpa.458568") is expected
    await adapter.aclose()


# ── NeurIPS proceedings: the publisher of record keyed by the cited URL ─────────────────────────

# The official export of AI4X_Schutt2017 (the AI4X-2027 Word template cites exactly this record).
_NEURIPS_BIB = r"""@inproceedings{NIPS2017_303ed4c6,
 author = {Sch\"{u}tt, Kristof and Kindermans, Pieter-Jan and Sauceda Felix, Huziel Enoc and Chmiela, Stefan and Tkatchenko, Alexandre and M\"{u}ller, Klaus-Robert},
 booktitle = {Advances in Neural Information Processing Systems},
 editor = {I. Guyon and U. Von Luxburg and S. Bengio and H. Wallach and R. Fergus and S. Vishwanathan and R. Garnett},
 pages = {},
 publisher = {Curran Associates, Inc.},
 title = {SchNet: A continuous-filter convolutional neural network for modeling quantum interactions},
 url = {https://proceedings.neurips.cc/paper_files/paper/2017/file/303ed4c69846ab36c2904d3ba8573050-Paper.pdf},
 volume = {30},
 year = {2017}
}
"""
_NEURIPS_HASH = "303ed4c69846ab36c2904d3ba8573050"
_NEURIPS_PDF = f"https://proceedings.neurips.cc/paper_files/paper/2017/file/{_NEURIPS_HASH}-Paper.pdf"
_NEURIPS_EXPORT = (
    f"https://proceedings.neurips.cc/paper_files/paper/2017/file/{_NEURIPS_HASH}-Bibtex.bib"
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (_NEURIPS_PDF, _NEURIPS_EXPORT),
        (f"https://proceedings.neurips.cc/paper/2017/hash/{_NEURIPS_HASH}-Abstract.html",
         _NEURIPS_EXPORT),
        ("https://proceedings.neurips.cc/paper_files/paper/2022/hash/"
         "002262941c9edfd472a79298b2ac5e17-Abstract-Conference.html",
         "https://proceedings.neurips.cc/paper_files/paper/2022/file/"
         "002262941c9edfd472a79298b2ac5e17-Bibtex.bib"),
        (f"https://papers.nips.cc/paper/2017/file/{_NEURIPS_HASH}-Paper.pdf",
         f"https://papers.nips.cc/paper_files/paper/2017/file/{_NEURIPS_HASH}-Bibtex.bib"),
        ("https://papers.nips.cc/paper/6700-schnet-a-continuous-filter", None),  # no paper hash
        ("https://openreview.net/forum?id=abc", None),
        (None, None),
    ],
)
def test_neurips_export_url_derivation(url, expected):
    assert neurips_export_url(url) == expected


async def test_neurips_proceedings_export_is_parsed_with_its_publisher():
    seen = []

    async def fetch(url):
        seen.append(url)
        return 200, _NEURIPS_BIB

    res = await PublisherAdapter(export_fetch=fetch).lookup_proceedings(_NEURIPS_PDF)
    assert seen == [_NEURIPS_EXPORT]
    assert res.error is None and res.query_kind == "web"
    rec = res.records[0]
    assert rec.publisher == "Curran Associates, Inc."
    assert rec.venue == "Advances in Neural Information Processing Systems"
    assert rec.year == 2017 and rec.volume == "30"


async def test_neurips_missing_export_is_empty_and_challenge_is_error():
    missing = await PublisherAdapter(export_fetch=_fake_export((404, ""))).lookup_proceedings(
        _NEURIPS_PDF
    )
    assert missing.records == [] and missing.error is None
    walled = await PublisherAdapter(export_fetch=_fake_export((200, _CHALLENGE))).lookup_proceedings(
        _NEURIPS_PDF
    )
    assert walled.records == [] and "not a citation export" in walled.error


_SCHNET_WORD_BIB = (
    "@inproceedings{AI4X_Schutt2017, "
    "title={SchNet: A continuous-filter convolutional neural network for modeling quantum "
    "interactions}, "
    "author={Sch{\\\"u}tt, Kristof and Kindermans, Pieter-Jan and Sauceda Felix, Huziel Enoc and "
    "Chmiela, Stefan and Tkatchenko, Alexandre and M{\\\"u}ller, Klaus-Robert}, "
    "booktitle={Advances in Neural Information Processing Systems}, "
    "publisher={Curran Associates, Inc.}, volume={30}, year={2017}, "
    f"url={{{_NEURIPS_PDF}}}}}"
)
_SCHNET_S2 = {
    "data": [{
        "paperId": "schnet",
        "title": "SchNet: A continuous-filter convolutional neural network for modeling quantum "
                 "interactions",
        "year": 2017,
        "venue": "Neural Information Processing Systems",
        "authors": [{"name": n} for n in (
            "Kristof T. Schütt", "Pieter-Jan Kindermans", "Huziel Enoc Sauceda Felix",
            "Stefan Chmiela", "Alexandre Tkatchenko", "Klaus-Robert Müller",
        )],
        "externalIds": {"ArXiv": "1706.08566", "DOI": "10.48550/arXiv.1706.08566"},
        "citationCount": 453,
    }]
}


async def _audit_schnet(tmp_path, export):
    from reference_audit.sources.semantic_scholar import SemanticScholarAdapter

    respx.get(url__startswith="https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(200, json=_SCHNET_S2)
    )
    respx.get(url__startswith="https://api.semanticscholar.org/graph/v1/paper/").mock(
        return_value=httpx.Response(200, json=_SCHNET_S2["data"][0])
    )
    respx.get(url__startswith="https://doi.org/").mock(
        return_value=httpx.Response(302, headers={"Location": "https://arxiv.org/abs/1706.08566"})
    )
    respx.get(url__startswith="https://arxiv.org/").mock(return_value=httpx.Response(200))
    bib = tmp_path / "r.bib"
    bib.write_text(_SCHNET_WORD_BIB, encoding="utf-8")
    pipe = AuditPipeline(
        AuditConfig(model="t", use_llm=False),
        adapters=[
            SemanticScholarAdapter(client=httpx.AsyncClient()),
            PublisherAdapter(client=httpx.AsyncClient(), export_fetch=_fake_export(export)),
        ],
        llm=None,
    )
    report = await pipe.run(None, bib)
    await pipe.aclose()
    return report.entries[0]


@respx.mock
async def test_cited_neurips_url_verifies_publisher_and_raises_no_doi_issue(tmp_path):
    # AI4X_Schutt2017: the canonical NeurIPS 2017 citation. The venue mints no DOI and the only DOI
    # found is the arXiv preprint's, so neither is an issue; the publisher no aggregator carries is
    # verified against the proceedings site's own BibTeX, keyed by the cited URL.
    audit = await _audit_schnet(tmp_path, (200, _NEURIPS_BIB))
    assert audit.verdict.kind == "exactly_one"
    by_field = {f.field: f for f in audit.field_findings}
    assert by_field["publisher"].status == "ok"
    assert by_field["publisher"].sources == ["publisher"]
    assert by_field["journal/venue"].status == "ok"
    assert audit.issues == []


@respx.mock
async def test_cited_neurips_url_of_another_paper_is_reported_not_used(tmp_path):
    other = _NEURIPS_BIB.replace(
        "SchNet: A continuous-filter convolutional neural network for modeling quantum interactions",
        "Attention Is All You Need",
    ).replace("Sch\\\"{u}tt, Kristof and Kindermans, Pieter-Jan", "Vaswani, Ashish and Shazeer, Noam")
    audit = await _audit_schnet(tmp_path, (200, other))
    assert audit.verdict.kind == "exactly_one"
    assert any(
        "'Attention Is All You Need'" in i and "could not be tied to the matched work" in i
        for i in audit.issues
    )
    by_field = {f.field: f for f in audit.field_findings}
    assert by_field["publisher"].status == "unverifiable"


@respx.mock
async def test_cited_neurips_url_without_export_is_reported(tmp_path):
    audit = await _audit_schnet(tmp_path, (404, ""))
    assert any("has no BibTeX export" in i for i in audit.issues)
