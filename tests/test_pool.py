"""Candidate pooling: identifier dedup + version-edge merge, without collapsing distinct works."""

from reference_audit.matching.pool import pool_candidates
from reference_audit.models import Identifiers, SourceRecord


def _rec(source, doi=None, arxiv=None, links=None, oa=None, cites=0, title="T", authors=None):
    return SourceRecord(
        source=source, title=title, authors=authors or ["A B"], citation_count=cites,
        ids=Identifiers(doi=doi, arxiv_id=arxiv), version_links=links or [], openalex_work_id=oa,
    )


def test_same_doi_from_two_sources_merges():
    recs = [_rec("crossref", doi="10.1/x", cites=5), _rec("openalex", doi="10.1/x", cites=9)]
    pooled = pool_candidates(recs)
    assert len(pooled) == 1
    assert pooled[0].citation_count == 9  # richer representative


def test_preprint_merges_with_published_via_version_link():
    # T1: published Work lists the arXiv preprint location; arXiv record carries that arxiv id
    published = _rec("openalex", doi="10.1177/03010066251384492",
                     links=["https://arxiv.org/abs/2408.04076"], cites=3)
    preprint = _rec("arxiv", arxiv="2408.04076", doi="10.48550/arxiv.2408.04076")
    pooled = pool_candidates([published, preprint])
    assert len(pooled) == 1  # one artifact, two versions
    assert pooled[0].ids.doi == "10.1177/03010066251384492"
    assert pooled[0].ids.arxiv_id == "2408.04076"


def test_distinct_dois_stay_separate():
    # T2b/T2c: consecutive DOIs, no version edge between them → never merge
    a = _rec("crossref", doi="10.1073/pnas.2120037119")
    b = _rec("crossref", doi="10.1073/pnas.2120042119")
    assert len(pool_candidates([a, b])) == 2


def test_preprint_published_merge_without_explicit_link():
    # zhang2018/plantec: sources return preprint (arXiv DOI) + published (CVPR DOI), no cross-link.
    # Same title + authors, exactly one a preprint → merge into one work.
    title = "The Unreasonable Effectiveness of Deep Features as a Perceptual Metric"
    authors = ["Richard Zhang", "Phillip Isola"]
    published = _rec("semantic_scholar", doi="10.1109/cvpr.2018.00068", title=title, authors=authors)
    preprint = _rec("openalex", doi="10.48550/arxiv.1801.03924", title=title, authors=authors)
    pooled = pool_candidates([published, preprint])
    assert len(pooled) == 1
    assert pooled[0].ids.doi == "10.1109/cvpr.2018.00068"  # published is the representative


def test_same_book_editions_merge():
    # gavrilets/mabook: same title+authors, no conflicting published DOIs → one work
    t = "Modern Theory of Critical Phenomena"
    a = _rec("openlibrary", title=t, authors=["Shang-Keng Ma"])  # no DOI
    b = _rec("crossref", doi="10.4324/9780429498886", title=t, authors=["Shang-keng Ma"])
    assert len(pool_candidates([a, b])) == 1


def test_two_distinct_published_dois_same_title_stay_separate():
    # fu2023: one paper with two genuinely distinct published DOIs → V1 holds (defer to M5 LLM)
    t = "DreamSim: Learning New Dimensions of Human Visual Similarity"
    au = ["Stephanie Fu", "Phillip Isola"]
    a = _rec("crossref", doi="10.52202/075280-2208", title=t, authors=au)
    b = _rec("semantic_scholar", doi="10.5555/3666122.3668330", title=t, authors=au)
    assert len(pool_candidates([a, b])) == 2


def test_two_distinct_published_titles_never_version_merge():
    # T2a-like: same authors, distinct DOIs, DIFFERENT titles, neither a preprint → stay separate
    a = _rec("crossref", doi="10.1/aaa", title="Multiscale structural complexity of natural patterns",
             authors=["Bagrov", "Katsnelson"])
    b = _rec("crossref", doi="10.2/bbb",
             title="Multiscale structural complexity as a quantitative measure of visual complexity",
             authors=["Kravchenko", "Bagrov", "Katsnelson"])
    assert len(pool_candidates([a, b])) == 2


def test_records_without_ids_stay_separate():
    a = _rec("x", title="Some paper")
    b = _rec("y", title="Another paper")
    assert len(pool_candidates([a, b])) == 2
