"""Candidate pooling: identifier dedup + version-edge merge, without collapsing distinct works."""

from reference_audit.matching.pool import pool_candidates
from reference_audit.models import Identifiers, SourceRecord


def _rec(source, doi=None, arxiv=None, links=None, oa=None, cites=0, title="T", authors=None,
         venue="", volume="", issue="", pages="", publisher="", year=None):
    return SourceRecord(
        source=source, title=title, authors=authors or ["A B"], citation_count=cites,
        venue=venue, volume=volume, issue=issue, pages=pages, publisher=publisher, year=year,
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


def test_representative_compiles_best_metadata_across_sources():
    # wolpert: S2 is the citation-richest record but truncates the venue ('Complex') and omits
    # volume/issue; crossref carries the full, correct metadata. The merged candidate must compile
    # crossref's values rather than inherit S2's poorer ones.
    s2 = _rec("semantic_scholar", doi="10.1/x", cites=99, venue="Complex")
    crossref = _rec("crossref", doi="10.1/x", cites=4, venue="Complexity",
                    volume="12", issue="3", pages="77-85")
    [rep] = pool_candidates([s2, crossref])
    assert rep.citation_count == 99       # identity still the richest record
    assert rep.venue == "Complexity"      # but venue compiled from crossref
    assert (rep.volume, rep.issue, rep.pages) == ("12", "3", "77-85")


def test_representative_fills_pages_from_openalex_when_crossref_blank():
    crossref = _rec("crossref", doi="10.1/x", cites=4, venue="PNAS", volume="115", issue="37")
    openalex = _rec("openalex", doi="10.1/x", cites=9, venue="PNAS", pages="E8678--E8687")
    [rep] = pool_candidates([crossref, openalex])
    assert rep.pages == "E8678--E8687"    # crossref had no pages → filled from openalex
    assert rep.volume == "115"            # crossref outranks openalex where both present


def test_transitive_merge_sources_published_venue_not_preprint():
    # Flow-Lenia (the provenance bug): the Semantic Scholar record carries BOTH the published DOI and
    # the arXiv id, so it bridges the published records and the arXiv-preprint record into ONE
    # candidate. Transitive closure must merge all four, and canonical venue/year must come from the
    # published side (crossref) — never the arXiv repository copy that the old greedy/2-phase pooling
    # let win.
    title = "Flow-Lenia: Towards open-ended evolution in cellular automata"
    au = ["Erwan Plantec", "Bert Chan"]
    cr = _rec("crossref", doi="10.1162/isal_a_00651", title=title, authors=au,
              venue="The 2023 Conference on Artificial Life", year=2023)
    oa_pub = _rec("openalex", doi="10.1162/isal_a_00651", title=title, authors=au, venue="", year=2023)
    oa_pre = _rec("openalex", doi="10.48550/arxiv.2212.07906", arxiv="2212.07906", title=title,
                  authors=au, venue="arXiv (Cornell University)", year=2022)
    s2 = _rec("semantic_scholar", doi="10.1162/isal_a_00651", arxiv="2212.07906", title=title,
              authors=au, venue="The 2023 Conference on Artificial Life", year=2022, cites=80)
    pooled = pool_candidates([cr, oa_pub, oa_pre, s2])
    assert len(pooled) == 1
    rep = pooled[0]
    assert rep.venue == "The 2023 Conference on Artificial Life"  # published, not the arXiv repo
    assert rep.year == 2023                                       # registrant year, not preprint 2022
    assert set(rep.raw["merged_from"]) == {"crossref", "openalex", "semantic_scholar"}


def test_representative_year_from_registrant_not_richest_record():
    # chan2019 'Lenia': S2 is citation-richest but reports the online year 2018; Crossref (the DOI
    # registrant) says 2019. The representative must carry 2019, not S2's 2018.
    s2 = _rec("semantic_scholar", doi="10.1/x", cites=500, year=2018)
    crossref = _rec("crossref", doi="10.1/x", cites=10, year=2019)
    [rep] = pool_candidates([s2, crossref])
    assert rep.citation_count == 500  # identity still the richest record
    assert rep.year == 2019           # but the year is the registrant's


# --- version edges are person-level; a pooled record keeps its members ----------------------------

_CF_TITLE = "CrossFormer: A Versatile Vision Transformer Hinging on Cross-scale Attention"
_CFPP_TITLE = "CrossFormer++: A Versatile Vision Transformer Hinging on Cross-Scale Attention"
_CF = ["Wenxiao Wang", "Lu Yao", "Long Chen", "Binbin Lin", "Deng Cai", "Xiaofei He", "Wei Liu"]
_CFPP = ["Wenxiao Wang", "Wei Chen", "Qibo Qiu", "Long Chen", "Boxi Wu", "Binbin Lin", "Xiaofei He",
         "Wei Liu"]


def test_a_conference_paper_is_not_fused_with_its_journal_extension():
    # HALLMARK f8d361220ec8: CrossFormer (ICLR, no DOI) and CrossFormer++ (TPAMI), each with an arXiv
    # copy. Short surnames made the fuzzy author overlap 0.85, so every pair had a version edge and
    # the ICLR record disappeared behind CrossFormer++'s metadata. Union-find order must not matter.
    import itertools

    iclr = _rec("dblp", title=_CF_TITLE + ".", authors=_CF, venue="ICLR", year=2022)
    cf_arxiv = _rec("openalex", doi="10.48550/arxiv.2108.00154", arxiv="2108.00154", title=_CF_TITLE,
                    authors=_CF, venue="arXiv (Cornell University)", year=2021, cites=9)
    tpami = _rec("openalex", doi="10.1109/tpami.2023.3341806", title=_CFPP_TITLE, authors=_CFPP,
                 venue="IEEE TPAMI", year=2023, cites=50)
    pp_arxiv = _rec("dblp", doi="10.48550/arxiv.2303.06908", arxiv="2303.06908", title=_CFPP_TITLE,
                    authors=_CFPP, venue="CoRR", year=2023)
    for order in itertools.permutations([iclr, cf_arxiv, tpami, pp_arxiv]):
        pooled = pool_candidates(list(order))
        with_iclr = next(p for p in pooled if any(m.venue == "ICLR" for m in p.members or [p]))
        assert all(m.authors == _CF for m in with_iclr.members or [with_iclr]), order


def test_a_pooled_venue_is_never_a_preprint_server_while_a_member_names_the_venue():
    # RODE (HALLMARK fbc5d48e8551): OpenAlex holds only the arXiv copy, DBLP the ICLR paper.
    authors = ["Tonghan Wang", "Tarun Gupta", "Anuj Mahajan"]
    oa = _rec("openalex", doi="10.48550/arxiv.2010.01523", arxiv="2010.01523", authors=authors,
              venue="arXiv (Cornell University)", year=2020, cites=40, title="RODE")
    dblp = _rec("dblp", venue="ICLR", authors=authors, year=2021, title="RODE")
    (pooled,) = pool_candidates([oa, dblp])
    assert pooled.venue == "ICLR"
    assert sorted(m.source for m in pooled.members) == ["dblp", "openalex"]
    assert all(not m.members and not m.raw for m in pooled.members)  # leaves, without raw


def test_members_stay_flat_when_a_pooled_record_is_pooled_again():
    # Enrichment re-pools the matched artifact with by-id records.
    a = _rec("crossref", doi="10.1/x", cites=5)
    b = _rec("openalex", doi="10.1/x", cites=9)
    (first,) = pool_candidates([a, b])
    (again,) = pool_candidates([first, _rec("semantic_scholar", doi="10.1/x")])
    assert sorted(m.source for m in again.members) == ["crossref", "openalex", "semantic_scholar"]
    assert all(not m.members for m in again.members)


def test_a_title_alone_never_makes_a_version():
    published = _rec("crossref", doi="10.1/x", title="Deep Learning", authors=["Yann LeCun"])
    preprint = SourceRecord(  # built directly: `_rec` would supply a default author
        source="arxiv", title="Deep Learning", authors=[],
        ids=Identifiers(arxiv_id="1234.5678", doi="10.48550/arxiv.1234.5678"),
    )
    assert preprint.authors == []
    assert len(pool_candidates([published, preprint])) == 2


def test_a_version_link_does_not_fuse_a_paper_with_its_journal_extension():
    # In the real CrossFormer pool an aggregator's version link tied the CrossFormer arXiv copy to
    # the CrossFormer++ TPAMI record; that link bypassed the guard while version edges did not.
    import itertools

    iclr = _rec("dblp", title=_CF_TITLE + ".", authors=_CF, venue="ICLR", year=2022)
    cf_arxiv = _rec("openalex", doi="10.48550/arxiv.2108.00154", arxiv="2108.00154", title=_CF_TITLE,
                    authors=_CF, venue="arXiv (Cornell University)", year=2021)
    tpami = _rec("openalex", doi="10.1109/tpami.2023.3341806", title=_CFPP_TITLE, authors=_CFPP,
                 venue="IEEE TPAMI", year=2023, cites=50,
                 links=["https://arxiv.org/abs/2108.00154"])  # the aggregator's wrong link
    for order in itertools.permutations([iclr, cf_arxiv, tpami]):
        pooled = pool_candidates(list(order))
        assert not any(
            {"ICLR", "IEEE TPAMI"} <= {m.venue for m in p.members or [p]} for p in pooled
        ), order


def test_a_record_compatible_with_both_works_does_not_bridge_them():
    # The real CrossFormer pool: a one-author Crossref supplementary-material DOI ('.../mm1') fits
    # both author lists, so it version-linked CrossFormer's cluster to CrossFormer++'s preprint,
    # which shares an arXiv id with the TPAMI record.
    import itertools

    supplement = _rec("crossref", doi="10.1109/tpami.2023.3341806/mm1", title=_CFPP_TITLE,
                      authors=["Wenxiao Wang"])
    iclr = _rec("dblp", title=_CF_TITLE + ".", authors=_CF, venue="ICLR", year=2022)
    pp_arxiv = _rec("openalex", doi="10.48550/arxiv.2303.06908", arxiv="2303.06908",
                    title=_CFPP_TITLE, authors=_CFPP, venue="arXiv (Cornell University)")
    tpami = _rec("semantic_scholar", doi="10.1109/tpami.2023.3341806", arxiv="2303.06908",
                 title=_CFPP_TITLE, authors=_CFPP, venue="IEEE TPAMI", cites=50)
    for order in itertools.permutations([supplement, iclr, pp_arxiv, tpami]):
        pooled = pool_candidates(list(order))
        assert not any(
            {"ICLR", "IEEE TPAMI"} <= {m.venue for m in p.members or [p]} for p in pooled
        ), order
