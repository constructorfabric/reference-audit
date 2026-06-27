"""DOI-less entry backfill + routing (M3)."""

import httpx
import respx

from reference_audit.cache.store import AuditCache
from reference_audit.config import AuditConfig
from reference_audit.models import BibEntry, EntryType, Identifiers
from reference_audit.pipeline import AuditPipeline
from reference_audit.sources.crossref import CrossrefAdapter
from reference_audit.sources.openlibrary import OpenLibraryAdapter
from reference_audit.sources.registry import route_entry


def test_route_doi_entry_uses_id_and_metadata():
    adapters = [CrossrefAdapter(), OpenLibraryAdapter()]
    entry = BibEntry(key="k", entry_type=EntryType.ARTICLE, title="T",
                     ids=Identifiers(doi="10.1/x"))
    route = route_entry(entry, adapters)
    assert any(a.name == "crossref" for a in route.id_adapters)
    assert any(a.name == "crossref" for a in route.metadata_adapters)


def test_route_book_uses_openlibrary_metadata():
    adapters = [CrossrefAdapter(), OpenLibraryAdapter()]
    entry = BibEntry(key="k", entry_type=EntryType.BOOK, title="A Book")
    route = route_entry(entry, adapters)
    assert any(a.name == "openlibrary" for a in route.metadata_adapters)


DOILESS_BIB = (
    "@inproceedings{zhang, "
    "title={The Unreasonable Effectiveness of Deep Features as a Perceptual Metric}, "
    "author={Zhang, Richard and Isola, Phillip}, year={2018}}"
)

CR_ITEM = {
    "DOI": "10.1109/cvpr.2018.00068",
    "title": ["The Unreasonable Effectiveness of Deep Features as a Perceptual Metric"],
    "author": [{"given": "Richard", "family": "Zhang"}, {"given": "Phillip", "family": "Isola"}],
    "container-title": ["CVPR"],
    "issued": {"date-parts": [[2018]]},
    "type": "proceedings-article",
}


@respx.mock
async def test_doiless_entry_backfills_doi(tmp_path):
    # No id endpoint is hit (entry has no id); metadata search returns the paper with a DOI.
    respx.get(url__regex=r"api\.crossref\.org/works\?").mock(
        return_value=httpx.Response(200, json={"message": {"items": [CR_ITEM]}})
    )
    bib = tmp_path / "r.bib"
    bib.write_text(DOILESS_BIB, encoding="utf-8")
    cache = AuditCache(tmp_path / "c.db", model="test")
    pipe = AuditPipeline(
        AuditConfig(model="test"), cache=cache,
        adapters=[CrossrefAdapter(client=httpx.AsyncClient())],
    )
    report = await pipe.run(None, bib)
    await pipe.aclose()
    cache.close()

    audit = report.entries[0]
    assert audit.verdict.kind == "exactly_one"
    assert any("DOI found via crossref: 10.1109/cvpr.2018.00068" in i for i in audit.issues)
