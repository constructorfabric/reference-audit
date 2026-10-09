"""HTML character references from web-scraped metadata are decoded before parsing and matching."""

from reference_audit.matching.features import title_ratio
from reference_audit.matching.names import author_overlap, authors_missing
from reference_audit.parsing.bib import parse_bib
from reference_audit.parsing.entities import decode_html_entities


def test_only_complete_references_are_decoded():
    assert decode_html_entities("d&apos;Amore &amp; Ch&#39;ng &#x41;") == "d'Amore & Ch'ng A"
    # a bare ampersand, or a legacy reference without its ';', is ordinary text
    assert decode_html_entities("R&D &copy 2020 &unknownword;") == "R&D &copy 2020 &unknownword;"


def test_bib_fields_are_decoded(tmp_path):
    bib = tmp_path / "r.bib"
    bib.write_text(
        "@inproceedings{k, title={Parameter Allocation &amp; Regularization},"
        " author={Francesco d&apos;Amore and Ken&apos;ichi Kumatani}, booktitle={AAAI}, year={2022}}\n",
        encoding="utf-8",
    )
    (entry,), _, _ = parse_bib(bib)
    assert entry.title == "Parameter Allocation & Regularization"
    assert entry.authors == ["Francesco d'Amore", "Ken'ichi Kumatani"]
    assert entry.raw_fields["author"].startswith("Francesco d&apos;Amore")  # raw stays raw


def test_an_encoded_author_matches_the_decoded_record():
    cited, record = ["Francesco d&apos;Amore", "Daniel Mitropolsky"], ["Francesco d'Amore",
                                                                         "Daniel Mitropolsky"]
    assert authors_missing(cited, [record]) == []
    assert author_overlap(cited, record) == 1.0


def test_an_encoded_title_matches_the_decoded_record():
    assert title_ratio("Allocation &amp; Regularization", "Allocation & Regularization") == 1.0
