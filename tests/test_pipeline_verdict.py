"""End-to-end verdicts with mocked Crossref (id + metadata) + cache. No real network.

Uses a single CrossrefAdapter so routing queries it both by-id and by-metadata; the metadata hit
pools into the same DOI as the id hit (one artifact).
"""

import httpx
import respx

from reference_audit.cache.store import AuditCache
from reference_audit.config import AuditConfig
from reference_audit.pipeline import AuditPipeline
from reference_audit.sources.crossref import CrossrefAdapter

REAL_DOI = "10.1073/pnas.2120037119"
REAL_ITEM = {
    "DOI": REAL_DOI,
    "title": ["Toward a theory of evolution as multilevel learning"],
    "author": [
        {"given": "Vitaly", "family": "Vanchurin"},
        {"given": "Yuri I.", "family": "Wolf"},
        {"given": "Mikhail I.", "family": "Katsnelson"},
        {"given": "Eugene V.", "family": "Koonin"},
    ],
    "container-title": ["Proceedings of the National Academy of Sciences"],
    "issued": {"date-parts": [[2022]]},
    "type": "journal-article",
}

BIB = """
@article{good,
  title={Toward a theory of evolution as multilevel learning},
  author={Vanchurin, Vitaly and Wolf, Yuri I. and Katsnelson, Mikhail I. and Koonin, Eugene V.},
  journal={Proceedings of the National Academy of Sciences}, year={2022},
  doi={10.1073/pnas.2120037119}}

@article{halluc,
  title={A completely invented paper that certainly does not exist anywhere},
  author={Nobody, A. B.}, journal={Journal of Nowhere}, year={2021},
  doi={10.9999/fake.0001}}
"""

ID_RE = r"https://api\.crossref\.org/works/10\."
SEARCH_RE = r"https://api\.crossref\.org/works\?"


def _pipeline(cache):
    return AuditPipeline(
        AuditConfig(model="test"),
        cache=cache,
        adapters=[CrossrefAdapter(client=httpx.AsyncClient())],
    )


def _bib(tmp_path, text=BIB):
    p = tmp_path / "r.bib"
    p.write_text(text, encoding="utf-8")
    return p


@respx.mock
async def test_real_and_hallucinated_verdicts(tmp_path):
    respx.get(url__regex=ID_RE).mock(
        side_effect=lambda req: httpx.Response(200, json={"message": REAL_ITEM})
        if REAL_DOI in str(req.url)
        else httpx.Response(404)
    )
    respx.get(url__regex=SEARCH_RE).mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={"message": {"items": [REAL_ITEM] if "multilevel" in str(req.url) else []}},
        )
    )
    cache = AuditCache(tmp_path / "c.db", model="test")
    pipe = _pipeline(cache)
    report = await pipe.run(None, _bib(tmp_path))
    await pipe.aclose()
    cache.close()

    by = {a.entry.key: a for a in report.entries}
    assert by["good"].verdict.kind == "exactly_one"
    assert len(by["good"].verdict.artifacts) == 1  # id + metadata pooled into one DOI
    assert by["halluc"].verdict.kind == "none"
    assert report.summary["verdicts"]["exactly_one"] == 1
    assert report.summary["verdicts"]["none"] == 1


@respx.mock
async def test_cache_prevents_second_query(tmp_path):
    id_route = respx.get(url__regex=ID_RE).mock(
        return_value=httpx.Response(200, json={"message": REAL_ITEM})
    )
    search_route = respx.get(url__regex=SEARCH_RE).mock(
        return_value=httpx.Response(200, json={"message": {"items": [REAL_ITEM]}})
    )
    bib = _bib(
        tmp_path,
        "@article{good, title={Toward a theory of evolution as multilevel learning}, "
        "author={Vanchurin, Vitaly}, year={2022}, doi={10.1073/pnas.2120037119}}",
    )
    cache = AuditCache(tmp_path / "c.db", model="test")

    pipe1 = _pipeline(cache)
    r1 = await pipe1.run(None, bib)
    await pipe1.aclose()
    assert r1.entries[0].verdict.kind == "exactly_one"
    calls_after_run1 = id_route.call_count + search_route.call_count
    assert calls_after_run1 >= 1

    pipe2 = _pipeline(cache)
    r2 = await pipe2.run(None, bib)
    await pipe2.aclose()
    cache.close()
    assert r2.entries[0].from_cache is True
    assert id_route.call_count + search_route.call_count == calls_after_run1  # zero new calls


@respx.mock
async def test_transient_error_does_not_flag_hallucination(tmp_path):
    respx.get(url__regex=ID_RE).mock(return_value=httpx.Response(503))
    respx.get(url__regex=SEARCH_RE).mock(return_value=httpx.Response(503))
    bib = _bib(
        tmp_path,
        "@article{good, title={Toward a theory of evolution as multilevel learning}, "
        "author={Vanchurin, Vitaly}, year={2022}, doi={10.1073/pnas.2120037119}}",
    )
    cache = AuditCache(tmp_path / "c.db", model="test")
    pipe = _pipeline(cache)
    report = await pipe.run(None, bib)
    await pipe.aclose()
    cache.close()
    # an outage leaves the entry UNRESOLVED, never a false 'none'
    assert report.entries[0].verdict is None
