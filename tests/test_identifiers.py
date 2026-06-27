"""Identifier normalization — including the real pilot quirks (T3)."""

from reference_audit.parsing.identifiers import (
    arxiv_to_doi,
    extract_arxiv_id,
    normalize_doi,
    normalize_isbn13,
)


def test_doi_strips_url_prefix_and_lowercases():
    # wolpert2007: DOI stored as a URL
    assert normalize_doi("https://doi.org/10.1002/cplx.20165") == "10.1002/cplx.20165"
    assert normalize_doi("http://dx.doi.org/10.1038/s41467-019-08746-5") == (
        "10.1038/s41467-019-08746-5"
    )
    # DOIs are case-insensitive → normalize to lowercase for opaque-token equality
    assert normalize_doi("10.25088/ComplexSystems.28.3.251") == (
        "10.25088/complexsystems.28.3.251"
    )


def test_doi_none_when_absent():
    assert normalize_doi(None) is None
    assert normalize_doi("no doi here") is None


def test_isbn10_converts_to_isbn13():
    assert normalize_isbn13("9780470050118") == "9780470050118"
    # ISBN-10 0262033844 -> ISBN-13 9780262033848 (with recomputed check digit)
    assert normalize_isbn13("0-262-03384-4") == "9780262033848"
    assert normalize_isbn13(None) is None
    assert normalize_isbn13("not-an-isbn") is None


def test_arxiv_from_eprint_field():
    assert extract_arxiv_id("2412.17799", "arXiv") == "2412.17799"
    assert extract_arxiv_id("2408.04076v2", "arXiv") == "2408.04076"  # version stripped


def test_arxiv_not_scraped_from_arbitrary_doi():
    # pugh2016quality: DOI 10.3389/frobt.2016.00040 must NOT yield arXiv 2016.00040
    assert extract_arxiv_id(None, None, fallback_text="10.3389/frobt.2016.00040") is None


def test_arxiv_from_marked_doi_or_url():
    assert extract_arxiv_id(None, None, fallback_text="10.48550/arXiv.2408.04076") == (
        "2408.04076"
    )
    assert extract_arxiv_id(None, None, fallback_text="https://arxiv.org/abs/2412.17799") == (
        "2412.17799"
    )


def test_arxiv_to_doi():
    assert arxiv_to_doi("2408.04076") == "10.48550/arxiv.2408.04076"
