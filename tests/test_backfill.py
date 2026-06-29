"""DOI-less entry backfill + routing (M3)."""

import httpx
import respx

from reference_audit.cache.store import AuditCache
from reference_audit.config import AuditConfig
from reference_audit.models import BibEntry, EntryType, Identifiers
from reference_audit.pipeline import AuditPipeline
from reference_audit.sources.crossref import CrossrefAdapter
from reference_audit.sources.openalex import OpenAlexAdapter
from reference_audit.sources.openlibrary import OpenLibraryAdapter
from reference_audit.sources.publisher import PublisherAdapter
from reference_audit.sources.registry import route_entry
from reference_audit.sources.semantic_scholar import SemanticScholarAdapter


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


def test_route_book_with_openalex_id_uses_openalex_lookup():
    # russell2019human: a book whose only id is an OpenAlex Work URL must reach the OpenAlex by-id
    # lookup (the article-centric search + Crossref/Open Library miss the trade title).
    adapters = [CrossrefAdapter(), OpenLibraryAdapter(), OpenAlexAdapter()]
    entry = BibEntry(key="k", entry_type=EntryType.BOOK, title="A Book",
                     ids=Identifiers(openalex="W3034344071"))
    route = route_entry(entry, adapters)
    assert any(a.name == "openalex" for a in route.id_adapters)


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


# A real production case (fu2023dreamsim): the work is genuinely on Semantic Scholar, but the only
# DOI it carries is the ACM `10.5555/...` placeholder the authors put in their arXiv metadata — and
# doi.org has never registered it. The match is correct; the backfilled DOI is not usable.
DREAMSIM_BIB = (
    "@inproceedings{fu2023dreamsim, "
    "title={DreamSim: Learning New Dimensions of Human Visual Similarity using Synthetic Data}, "
    "author={Fu, Stephanie and Tamir, Netanel and Sundaram, Shobhita}, year={2023}}"
)
S2_SEARCH = {
    "data": [
        {
            "paperId": "abc123",
            "title": "DreamSim: Learning New Dimensions of Human Visual Similarity using Synthetic Data",
            "year": 2023,
            "venue": "Neural Information Processing Systems",
            "authors": [
                {"name": "Stephanie Fu"},
                {"name": "Netanel Tamir"},
                {"name": "Shobhita Sundaram"},
            ],
            "externalIds": {"DOI": "10.5555/3666122.3668330"},
            "citationCount": 42,
        }
    ]
}


@respx.mock
async def test_backfilled_doi_that_does_not_resolve_is_flagged(tmp_path):
    # The work matches via S2 (exactly_one), but doi.org returns 404 for the backfilled DOI: it must
    # be reported as unusable, never as a clean "DOI found".
    respx.get(url__startswith="https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(200, json=S2_SEARCH)
    )
    respx.get("https://doi.org/10.5555/3666122.3668330").mock(
        return_value=httpx.Response(404, text="DOI Not Found")
    )
    bib = tmp_path / "r.bib"
    bib.write_text(DREAMSIM_BIB, encoding="utf-8")
    pipe = AuditPipeline(
        AuditConfig(model="test", use_llm=False, check_fields=False),
        adapters=[
            SemanticScholarAdapter(client=httpx.AsyncClient()),
            PublisherAdapter(client=httpx.AsyncClient()),
        ],
    )
    report = await pipe.run(None, bib)
    await pipe.aclose()

    audit = report.entries[0]
    assert audit.verdict.kind == "exactly_one"
    assert any("does NOT resolve at doi.org" in i for i in audit.issues)
    # ...and it is NOT echoed as a clean find.
    assert not any(
        i == "DOI found via semantic_scholar: 10.5555/3666122.3668330" for i in audit.issues
    )


@respx.mock
async def test_backfilled_doi_that_resolves_is_reported_clean(tmp_path):
    # Same flow, but doi.org redirects (handle found): the backfilled DOI is real, so it is reported
    # plainly with no resolution warning.
    respx.get(url__startswith="https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(200, json=S2_SEARCH)
    )
    respx.get("https://doi.org/10.5555/3666122.3668330").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.org/paper"})
    )
    bib = tmp_path / "r.bib"
    bib.write_text(DREAMSIM_BIB, encoding="utf-8")
    pipe = AuditPipeline(
        AuditConfig(model="test", use_llm=False, check_fields=False),
        adapters=[
            SemanticScholarAdapter(client=httpx.AsyncClient()),
            PublisherAdapter(client=httpx.AsyncClient()),
        ],
    )
    report = await pipe.run(None, bib)
    await pipe.aclose()

    audit = report.entries[0]
    assert audit.verdict.kind == "exactly_one"
    assert any(
        i == "DOI found via semantic_scholar: 10.5555/3666122.3668330" for i in audit.issues
    )
