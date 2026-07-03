"""Step 3 (Citation Alignment): abstracts flow from sources onto the matched artifact.

Offline — normalizers are pure over mocked API payloads; pooling is pure over records.
"""

from __future__ import annotations

from reference_audit.matching.pool import pool_candidates
from reference_audit.models import Identifiers, SourceRecord
from reference_audit.sources.normalize import (
    _openalex_abstract,
    openalex_work_to_record,
    s2_paper_to_record,
)


def test_openalex_abstract_reconstructed_from_inverted_index():
    work = {
        "id": "https://openalex.org/W1",
        "title": "Lenia",
        "abstract_inverted_index": {"Lenia": [0], "is": [1], "a": [2], "lifeform": [3]},
    }
    assert _openalex_abstract(work) == "Lenia is a lifeform"
    assert openalex_work_to_record(work).abstract == "Lenia is a lifeform"


def test_openalex_missing_abstract_is_empty_never_guessed():
    assert _openalex_abstract({"id": "x"}) == ""
    assert _openalex_abstract({"abstract_inverted_index": None}) == ""
    assert openalex_work_to_record({"id": "x", "title": "t"}).abstract == ""


def test_s2_abstract_passed_through():
    paper = {"paperId": "p1", "title": "T", "abstract": "We show that X implies Y."}
    assert s2_paper_to_record(paper).abstract == "We show that X implies Y."
    # absent abstract → empty, not None
    assert s2_paper_to_record({"paperId": "p2", "title": "T"}).abstract == ""


def test_pooling_preserves_abstract_when_richest_record_lacks_it():
    # Same DOI → the two records pool. The citation-richest (crossref) carries NO abstract; the
    # abstract must be compiled from the openalex record onto the merged representative.
    crossref = SourceRecord(
        source="crossref", ids=Identifiers(doi="10.1/x"), citation_count=100, abstract=""
    )
    openalex = SourceRecord(
        source="openalex", ids=Identifiers(doi="10.1/x"), citation_count=5,
        abstract="The reconstructed abstract.",
    )
    pooled = pool_candidates([crossref, openalex])
    assert len(pooled) == 1
    assert pooled[0].abstract == "The reconstructed abstract."


def test_pooling_keeps_the_fullest_abstract():
    a = SourceRecord(source="openalex", ids=Identifiers(doi="10.1/y"), abstract="short")
    b = SourceRecord(
        source="semantic_scholar", ids=Identifiers(doi="10.1/y"),
        abstract="a considerably longer and more complete abstract",
    )
    (rep,) = pool_candidates([a, b])
    assert rep.abstract == "a considerably longer and more complete abstract"
