"""A cited OpenAlex Work id is checked even next to a DOI (issue #3), and a Work id that is not the
entry's work is reported, whatever the verdict (issue #4). Mocked Crossref + OpenAlex."""

from __future__ import annotations

import httpx
import respx

from reference_audit.cache.store import AuditCache
from reference_audit.config import AuditConfig
from reference_audit.pipeline import AuditPipeline
from reference_audit.sources.crossref import CrossrefAdapter
from reference_audit.sources.openalex import OpenAlexAdapter

_TITLE = "Deep Residual Learning for Image Recognition"
_WORK_ID = "W2194775991"

# The cited DOI is wrong (another paper's); the cited Work id is the right one.
_BIB = (
    "@article{he2016deep, "
    f"title={{{_TITLE}}}, author={{He, Kaiming and Zhang, Xiangyu}}, year={{2016}}, "
    "doi={10.1016/j.bee.1999.01.001}, "
    f"url={{https://openalex.org/{_WORK_ID}}}}}"
)
_RIGHT_WORK = {
    "id": f"https://openalex.org/{_WORK_ID}",
    "doi": "https://doi.org/10.1109/cvpr.2016.90",
    "ids": {"openalex": f"https://openalex.org/{_WORK_ID}",
            "doi": "https://doi.org/10.1109/cvpr.2016.90"},
    "title": _TITLE,
    "publication_year": 2016,
    "type": "article",
    "authorships": [{"author": {"display_name": "Kaiming He"}},
                    {"author": {"display_name": "Xiangyu Zhang"}}],
}
_DOI_WORK = {
    "id": "https://openalex.org/W999",
    "doi": "https://doi.org/10.1016/j.bee.1999.01.001",
    "ids": {"openalex": "https://openalex.org/W999", "doi": "https://doi.org/10.1016/j.bee.1999.01.001"},
    "title": "An Entirely Unrelated Treatise on Beekeeping",
    "publication_year": 1999,
    "type": "article",
    "authorships": [{"author": {"display_name": "Someone Else"}}],
}
_CR_DOI_ITEM = {
    "DOI": "10.1016/j.bee.1999.01.001",
    "title": ["An Entirely Unrelated Treatise on Beekeeping"],
    "author": [{"given": "Someone", "family": "Else"}],
    "issued": {"date-parts": [[1999]]},
    "type": "journal-article",
}


def _mock_common() -> None:
    respx.get(url__regex=r"api\.crossref\.org/works\?").mock(
        return_value=httpx.Response(200, json={"message": {"items": []}})
    )
    respx.get(url__regex=r"api\.crossref\.org/works/10\.1016/j\.bee\.1999\.01\.001").mock(
        return_value=httpx.Response(200, json={"message": _CR_DOI_ITEM})
    )
    # The matched work's own DOI, fetched by the enrichment pass: not needed here.
    respx.get(url__regex=r"api\.crossref\.org/works/10\.1109/").mock(
        return_value=httpx.Response(404)
    )
    respx.get(url__regex=r"api\.openalex\.org/works\?").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    respx.get(url__regex=r"api\.openalex\.org/works/doi:10\.1016/j\.bee\.1999\.01\.001").mock(
        return_value=httpx.Response(200, json=_DOI_WORK)
    )


async def _run(tmp_path, bib_text: str, cache: AuditCache | None = None):
    bib = tmp_path / "r.bib"
    bib.write_text(bib_text, encoding="utf-8")
    pipe = AuditPipeline(
        AuditConfig(model="t", use_llm=False), cache=cache,
        adapters=[CrossrefAdapter(client=httpx.AsyncClient()),
                  OpenAlexAdapter(client=httpx.AsyncClient())],
    )
    report = await pipe.run(None, bib)
    await pipe.aclose()
    return report.entries[0]


@respx.mock
async def test_work_id_next_to_a_wrong_doi_is_looked_up_and_reports_contradiction(tmp_path):
    # Issue #3: OpenAlex used to be queried by the DOI only, so the Work id was never checked and
    # this real paper (cited with another paper's DOI) came out NO MATCH.
    _mock_common()
    work = respx.get(url__regex=rf"api\.openalex\.org/works/{_WORK_ID}").mock(
        return_value=httpx.Response(200, json=_RIGHT_WORK)
    )
    audit = await _run(tmp_path, _BIB)

    assert work.called
    assert audit.verdict.kind == "contradiction"
    assert audit.verdict.artifacts[0].best_record.ids.doi == _CR_DOI_ITEM["DOI"]
    assert "Beekeeping" in audit.verdict.rationale
    assert any(f.field == "author" and f.status == "error" for f in audit.field_findings)
    assert not any("OpenAlex Work id" in i for i in audit.issues)


@respx.mock
async def test_work_id_of_another_work_is_reported_also_from_cache(tmp_path):
    # Issue #4: the DOI is right, so the entry is matched — but its own Work id is another work's,
    # and that must be reported, on a cached run too.
    bib = (
        f"@article{{he2016deep, title={{{_TITLE}}}, author={{He, Kaiming and Zhang, Xiangyu}}, "
        "year={2016}, doi={10.1109/cvpr.2016.90}, url={https://openalex.org/W4242}}"
    )
    respx.get(url__regex=r"api\.crossref\.org/works\?").mock(
        return_value=httpx.Response(200, json={"message": {"items": []}})
    )
    respx.get(url__regex=r"api\.crossref\.org/works/10\.1109/cvpr\.2016\.90").mock(
        return_value=httpx.Response(404)
    )
    respx.get(url__regex=r"api\.openalex\.org/works\?").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    respx.get(url__regex=r"api\.openalex\.org/works/doi:10\.1109/cvpr\.2016\.90").mock(
        return_value=httpx.Response(200, json=_RIGHT_WORK)
    )
    respx.get(url__regex=r"api\.openalex\.org/works/W4242").mock(
        return_value=httpx.Response(200, json=dict(
            _DOI_WORK, id="https://openalex.org/W4242",
            ids={"openalex": "https://openalex.org/W4242"},
        ))
    )
    cache = AuditCache(tmp_path / "c.db", model="t")
    try:
        fresh = await _run(tmp_path, bib, cache)
        cached = await _run(tmp_path, bib, cache)
    finally:
        cache.close()

    assert fresh.verdict.kind == "contradiction"
    assert fresh.verdict.artifacts[0].best_record.ids.openalex == "W4242"
    assert cached.from_cache
    for audit in (fresh, cached):
        notes = [i for i in audit.issues if "Hard ID openalex:W4242" in i]
        assert len(notes) == 1
        assert "resolves to 'An Entirely Unrelated Treatise" in notes[0]


@respx.mock
async def test_work_id_only_mismatch_is_reported_with_no_match(tmp_path):
    # Issue #4 with no other identifier: the Work id is the only lead and it is another work.
    bib = (
        f"@article{{he2016deep, title={{{_TITLE}}}, author={{He, Kaiming and Zhang, Xiangyu}}, "
        f"year={{2016}}, url={{https://openalex.org/{_WORK_ID}}}}}"
    )
    _mock_common()
    respx.get(url__regex=rf"api\.openalex\.org/works/{_WORK_ID}").mock(
        return_value=httpx.Response(200, json=_DOI_WORK)  # answered as merged into W999
    )
    audit = await _run(tmp_path, bib)

    assert audit.verdict is None or audit.verdict.kind != "exactly_one"
    assert any(
        f"OpenAlex Work id {_WORK_ID} belongs to a different work (as W999)" in i
        and "Beekeeping" in i
        for i in audit.issues
    )


@respx.mock
async def test_work_id_that_does_not_resolve_is_reported(tmp_path):
    _mock_common()
    respx.get(url__regex=rf"api\.openalex\.org/works/{_WORK_ID}").mock(
        return_value=httpx.Response(404, json={})
    )
    audit = await _run(tmp_path, _BIB)

    assert any(
        f"OpenAlex Work id {_WORK_ID} does not resolve" in i for i in audit.issues
    )


@respx.mock
async def test_work_id_lookup_failure_is_reported(tmp_path):
    _mock_common()
    respx.get(url__regex=rf"api\.openalex\.org/works/{_WORK_ID}").mock(
        side_effect=httpx.ConnectError("All connection attempts failed")
    )
    audit = await _run(tmp_path, _BIB)

    assert any(
        f"could not check the cited OpenAlex Work id {_WORK_ID}" in i for i in audit.issues
    )
