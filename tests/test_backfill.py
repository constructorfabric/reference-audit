"""DOI-less entry backfill + routing (M3)."""

import httpx
import pytest
import respx

from reference_audit.cache.store import AuditCache
from reference_audit.config import AuditConfig
from reference_audit.models import BibEntry, EntryType, Identifiers
from reference_audit.pipeline import AuditPipeline
from reference_audit.sources.crossref import CrossrefAdapter
from reference_audit.sources.openalex import OpenAlexAdapter
from reference_audit.sources.openlibrary import OpenLibraryAdapter
from reference_audit.sources.publisher import PublisherAdapter
from reference_audit.sources.registry import route_entry, venue_allows_no_doi
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
    # The work matches via S2 (exactly_one), but the handle system does not know the backfilled
    # DOI: it must be reported as unusable, never as a clean "DOI found".
    respx.get(url__startswith="https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(200, json=S2_SEARCH)
    )
    respx.get(url__startswith="https://doi.org/api/handles/10.5555/3666122.3668330").mock(
        return_value=httpx.Response(
            404, json={"responseCode": 100, "handle": "10.5555/3666122.3668330"}
        )
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
    # Same flow, but the handle is found: the backfilled DOI is real, so it is reported plainly
    # with no resolution warning.
    respx.get(url__startswith="https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(200, json=S2_SEARCH)
    )
    respx.get(url__startswith="https://doi.org/api/handles/10.5555/3666122.3668330").mock(
        return_value=httpx.Response(200, json={
            "responseCode": 1, "handle": "10.5555/3666122.3668330",
            "values": [{"type": "URL", "data": {"value": "https://example.org/paper"}}],
        })
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


# ── arXiv DataCite DOIs and venues that mint no DOI ─────────────────────────────────────────────

ROADMAP_BIB = (
    "@misc{AI4X_Roadmap2025, "
    "title={AI4X roadmap: Artificial intelligence for the advancement of scientific pursuit and "
    "its future directions}, author={Dale, Stephen G. and Kazeev, Nikita}, year={2025}, "
    "eprint={2511.20976}, archivePrefix={arXiv}, url={https://arxiv.org/abs/2511.20976}}"
)
ROADMAP_S2 = {
    "paperId": "283261846",
    "title": "AI4X Roadmap: Artificial Intelligence for the advancement of scientific pursuit and "
             "its future directions",
    "year": 2025,
    "venue": "arXiv.org",
    "authors": [{"name": "Stephen G. Dale"}, {"name": "Nikita Kazeev"}],
    "externalIds": {"ArXiv": "2511.20976", "DOI": "10.48550/arXiv.2511.20976"},
    "citationCount": 0,
}


async def _audit_s2(tmp_path, bib_text, paper):
    respx.get(url__startswith="https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(200, json={"data": [paper]})
    )
    respx.get(url__startswith="https://api.semanticscholar.org/graph/v1/paper/").mock(
        return_value=httpx.Response(200, json=paper)
    )
    respx.get(url__startswith="https://doi.org/api/handles/").mock(
        return_value=httpx.Response(200, json={"responseCode": 1})
    )
    bib = tmp_path / "r.bib"
    bib.write_text(bib_text, encoding="utf-8")
    pipe = AuditPipeline(
        AuditConfig(model="test", use_llm=False, check_fields=False),
        adapters=[
            SemanticScholarAdapter(client=httpx.AsyncClient()),
            PublisherAdapter(client=httpx.AsyncClient()),
        ],
    )
    report = await pipe.run(None, bib)
    await pipe.aclose()
    return report.entries[0]


@respx.mock
async def test_arxiv_doi_of_the_cited_arxiv_id_is_not_a_find(tmp_path):
    # AI4X_Roadmap2025: a pure arXiv preprint cited by its arXiv id. 10.48550/arXiv.2511.20976 is
    # that same id in DOI form, so there is nothing to add.
    audit = await _audit_s2(tmp_path, ROADMAP_BIB, ROADMAP_S2)
    assert audit.verdict.kind == "exactly_one"
    assert audit.issues == []


@respx.mock
async def test_arxiv_doi_is_still_reported_for_a_journal_article_without_ids(tmp_path):
    # A journal mints DOIs: the published version's DOI was not found, and the arXiv DOI found
    # instead stays reported (and so does the "no DOI" warning).
    bib = (
        "@article{dale, title={AI4X roadmap: Artificial intelligence for the advancement of "
        "scientific pursuit and its future directions}, author={Dale, Stephen G. and Kazeev, "
        "Nikita}, journal={Nature Reviews Physics}, year={2025}}"
    )
    audit = await _audit_s2(tmp_path, bib, ROADMAP_S2)
    assert audit.verdict.kind == "exactly_one"
    assert "no DOI/arXiv id (will attempt DOI backfill)" in audit.issues
    assert "DOI found via semantic_scholar: 10.48550/arxiv.2511.20976" in audit.issues


@respx.mock
async def test_published_doi_at_an_allowlisted_venue_is_still_reported(tmp_path):
    # NeurIPS has minted Curran DOIs since 2022: an allowlisted venue never hides a DOI that exists.
    paper = {
        **ROADMAP_S2,
        "venue": "Neural Information Processing Systems",
        "externalIds": {"DOI": "10.52202/068431-0001"},
    }
    bib = (
        "@inproceedings{dale, title={AI4X roadmap: Artificial intelligence for the advancement of "
        "scientific pursuit and its future directions}, author={Dale, Stephen G. and Kazeev, "
        "Nikita}, booktitle={Advances in Neural Information Processing Systems}, year={2025}}"
    )
    audit = await _audit_s2(tmp_path, bib, paper)
    assert "no DOI/arXiv id (will attempt DOI backfill)" in audit.issues
    assert "DOI found via semantic_scholar: 10.52202/068431-0001" in audit.issues


@pytest.mark.parametrize(
    ("venue", "allowed"),
    [
        ("Advances in Neural Information Processing Systems", True),
        ("NeurIPS 2023 Datasets and Benchmarks Track", True),
        ("Proceedings of the 34th International Conference on Machine Learning", True),
        ("International Conference on Machine Learning, ICML 2017", True),
        ("International Conference on Learning Representations", True),
        ("Journal of Machine Learning Research", True),
        ("Transactions on Machine Learning Research", True),
        ("2017 16th IEEE International Conference on Machine Learning and Applications (ICMLA)",
         False),
        ("International Conference on Machine Learning, Optimization, and Data Science", False),
        ("International Conference on Neural Information Processing", False),  # ICONIP (Springer)
        ("Nature", False),
        ("", False),
    ],
)
def test_no_doi_venue_allowlist(venue, allowed):
    assert venue_allows_no_doi(venue) is allowed
