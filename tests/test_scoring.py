"""Feature computation + bucketing — the formal guards that keep T2 from auto-accepting."""

from reference_audit.config import AuditConfig
from reference_audit.matching.features import compute_features
from reference_audit.matching.scoring import bucket
from reference_audit.models import BibEntry, EntryType, Identifiers, SourceRecord

CFG = AuditConfig()
TT = CFG.prefix_trap_tail_jaccard


def _entry(**kw) -> BibEntry:
    return BibEntry(key="k", entry_type=EntryType.ARTICLE, **kw)


def test_auto_accept_on_doi_title_author_match():
    e = _entry(
        title="Toward a theory of evolution as multilevel learning",
        authors=["Vanchurin, Vitaly", "Wolf, Yuri I."],
        year=2022,
        ids=Identifiers(doi="10.1073/pnas.2120037119"),
    )
    r = SourceRecord(
        source="crossref",
        title="Toward a theory of evolution as multilevel learning",
        authors=["Vitaly Vanchurin", "Yuri I. Wolf"],
        year=2022,
        ids=Identifiers(doi="10.1073/pnas.2120037119"),
    )
    f = compute_features(e, r, tail_threshold=TT)
    assert f.id_agreement == "match"
    assert bucket(f, CFG) == "auto_accept"


def test_prefix_trap_blocks_auto_accept():
    # T2a: bagrov2020 vs kravchenko2026 — shared prefix, divergent tails, distinct DOIs
    e = _entry(
        title="Multiscale structural complexity of natural patterns",
        authors=["Bagrov, Andrey A.", "Katsnelson, Mikhail I."],
        year=2020,
        ids=Identifiers(doi="10.1073/pnas.2004976117"),
    )
    r = SourceRecord(
        source="crossref",
        title="Multiscale structural complexity as a quantitative measure of visual complexity",
        authors=["Anna Kravchenko", "Andrey A. Bagrov", "Mikhail I. Katsnelson"],
        year=2026,
        ids=Identifiers(doi="10.1177/03010066251384492"),
    )
    f = compute_features(e, r, tail_threshold=TT)
    assert f.title_prefix_trap is True
    assert f.id_agreement == "conflict"  # both DOIs present and different
    assert bucket(f, CFG) != "auto_accept"


def test_auto_reject_unrelated_low_composite():
    e = _entry(title="A study of feline behavior", authors=["Nobody, A."], year=1999,
               ids=Identifiers(doi="10.1/aaa"))
    r = SourceRecord(source="crossref", title="Quantum chromodynamics on the lattice",
                     authors=["Other, B."], year=2010, ids=Identifiers(doi="10.2/bbb"))
    f = compute_features(e, r, tail_threshold=TT)
    assert bucket(f, CFG) == "auto_reject"


def test_pages_conflict_detected():
    # T2c: laughlin1 vs laughlin2 — same venue/year, disjoint consecutive pages
    e = _entry(title="The Theory of Everything", authors=["Laughlin, R. B.", "Pines, David"],
               year=2000, venue="Proceedings of the National Academy of Sciences",
               pages="28--31", ids=Identifiers(doi="10.1073/pnas.97.1.28"))
    r = SourceRecord(source="crossref", title="The Middle Way",
                     authors=["R. B. Laughlin", "David Pines", "Joerg Schmalian"], year=2000,
                     venue="Proceedings of the National Academy of Sciences", pages="32-37",
                     ids=Identifiers(doi="10.1073/pnas.97.1.32"))
    f = compute_features(e, r, tail_threshold=TT)
    assert f.pages_conflict is True
    assert f.author_set_jaccard < 1.0  # laughlin/pines ⊂ 5-author set


def test_wrong_pages_does_not_block_exact_title_author_match():
    # soros2014: the citation has WRONG pages (306--313) for a paper that is actually at 793--800.
    # Title + authors are identical and the entry carries no id, so this is a backfill (Path B). The
    # page disagreement is a field error to report, NOT grounds to force adjudication / reject the
    # paper as a possible hallucination.
    e = _entry(
        title="Identifying Necessary Conditions for the Emergence of Open-Ended Evolution "
        "Through the Artificial Life World of Chromaria",
        authors=["Soros, Lisa B.", "Stanley, Kenneth O."],
        year=2014,
        venue="Proceedings of the Fourteenth International Conference on the Synthesis and "
        "Simulation of Living Systems (ALIFE 14)",
        pages="306--313",
    )
    r = SourceRecord(
        source="crossref",
        title="Identifying Necessary Conditions for Open-Ended Evolution through the "
        "Artificial Life World of Chromaria",
        authors=["L. Soros", "Kenneth O. Stanley"],
        year=2014,
        venue="Artificial Life 14: Proceedings of the Fourteenth International Conference on "
        "the Synthesis and Simulation of Living Systems",
        pages="793-800",
        ids=Identifiers(doi="10.1162/978-0-262-32621-6-ch128"),
    )
    f = compute_features(e, r, tail_threshold=TT)
    assert f.pages_conflict is True  # raw signal still fires (the citation's pages are wrong)
    assert f.title_ratio >= CFG.title_backfill and f.author_overlap >= CFG.author_accept
    # ...but it must NOT veto a title+author-identical backfill match.
    assert bucket(f, CFG, entry_has_id=False) == "auto_accept"


def test_pages_conflict_still_vetoes_when_titles_differ():
    # Guard the laughlin T2c case: when the titles are NOT near-exact, a disjoint page range in the
    # same venue still forces adjudication (the relaxation above is title+author gated).
    e = _entry(title="The Theory of Everything", authors=["Laughlin, R. B.", "Pines, David"],
               year=2000, venue="Proceedings of the National Academy of Sciences",
               pages="28--31")
    r = SourceRecord(source="crossref", title="The Middle Way",
                     authors=["R. B. Laughlin", "David Pines"], year=2000,
                     venue="Proceedings of the National Academy of Sciences", pages="32-37",
                     ids=Identifiers(doi="10.1073/pnas.97.1.32"))
    f = compute_features(e, r, tail_threshold=TT)
    assert f.pages_conflict is True
    assert bucket(f, CFG, entry_has_id=False) != "auto_accept"


def test_db_missing_author_still_accepts_on_doi_and_title():
    # wilson1974: Crossref drops the 2nd author (Kogut); exact DOI + title should still accept
    e = _entry(title="The renormalization group and the epsilon expansion",
               authors=["Wilson, K. G.", "Kogut, J."], year=1974,
               ids=Identifiers(doi="10.1016/0370-1573(74)90023-4"))
    r = SourceRecord(source="crossref", title="The renormalization group and the epsilon expansion",
                     authors=["K Wilson"], year=1974,
                     ids=Identifiers(doi="10.1016/0370-1573(74)90023-4"))
    f = compute_features(e, r, tail_threshold=TT)
    assert f.author_subset is True
    assert f.author_overlap < CFG.author_accept  # dragged down by the missing Kogut
    assert bucket(f, CFG) == "auto_accept"


def test_arxiv_doi_not_a_published_doi_conflict():
    # faldor: entry has the published MIT-Press DOI; a found arXiv preprint has the arXiv DOI.
    # That is a version relationship, NOT a conflict.
    from reference_audit.matching.features import id_agreement

    entry_ids = Identifiers(doi="10.1162/isal_a_00827")
    preprint_ids = Identifiers(doi="10.48550/arxiv.2406.04235", arxiv_id="2406.04235")
    assert id_agreement(entry_ids, preprint_ids) == "absent"  # not "conflict"


def test_openalex_work_id_match_auto_accepts():
    # russell2019human: cited only by an OpenAlex Work id; the resolved Work matches title+author.
    # A Work-id agreement is a strong identity signal → Path A auto-accept (no DOI/ISBN needed).
    e = _entry(
        title="Human Compatible: Artificial Intelligence and the Problem of Control",
        authors=["Stuart Russell"],
        year=2019,
        ids=Identifiers(openalex="W3034344071"),
    )
    r = SourceRecord(
        source="openalex",
        title="Human Compatible: Artificial Intelligence and the Problem of Control",
        authors=["Stuart Russell"],
        year=2019,
        ids=Identifiers(openalex="W3034344071"),
        openalex_work_id="https://openalex.org/W3034344071",
    )
    f = compute_features(e, r, tail_threshold=TT)
    assert f.id_agreement == "match"
    assert bucket(f, CFG) == "auto_accept"


def test_openalex_work_id_mismatch_is_conflict():
    # A different Work id returned by metadata search is a distinct work, not the cited one.
    from reference_audit.matching.features import id_agreement

    assert id_agreement(Identifiers(openalex="W1"), Identifiers(openalex="W2")) == "conflict"


def test_subset_title_not_inflated():
    # "Fitness Landscapes" (a different chapter) must NOT score 1.0 against the full book title
    e = _entry(title="Fitness Landscapes and the Origin of Species", authors=["Gavrilets, Sergey"])
    r = SourceRecord(source="crossref", title="Fitness Landscapes", authors=["Sergey Gavrilets"],
                     ids=Identifiers(doi="10.1016/chapter"))
    f = compute_features(e, r, tail_threshold=TT)
    assert f.title_ratio < 0.95  # length-guarded; was 1.0 with bare token_set_ratio


def test_year_gap_does_not_hard_gate():
    # preprint(2024) vs published(2026): year_factor stays high, never zero
    f_year = compute_features(
        _entry(title="X", authors=["A B"], year=2024, ids=Identifiers(doi="10.1/x")),
        SourceRecord(source="crossref", title="X", authors=["A B"], year=2026,
                     ids=Identifiers(doi="10.1/x")),
        tail_threshold=TT,
    )
    assert f_year.year_factor > 0.5
