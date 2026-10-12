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


def test_source_record_fields_are_decoded():
    from reference_audit.models import SourceRecord

    rec = SourceRecord(
        source="crossref",
        title="Allocation &amp; Regularization",
        authors=["Francesco d&apos;Amore"],
        venue="Proc. AAAI &amp; IAAI",
        publisher="Taylor &amp; Francis",
    )
    assert rec.title == "Allocation & Regularization"
    assert rec.authors == ["Francesco d'Amore"]
    assert rec.venue == "Proc. AAAI & IAAI"
    assert rec.publisher == "Taylor & Francis"
    # a record reloaded from the source cache is decoded too
    cached = '{"source": "crossref", "title": "Allocation &amp; Regularization"}'
    assert SourceRecord.model_validate_json(cached).title == "Allocation & Regularization"


def test_an_encoded_source_title_is_settled_by_the_field_check():
    from reference_audit.fieldcheck import deterministic_field_checks
    from reference_audit.models import BibEntry, EntryType, Identifiers, MatchedArtifact, SourceRecord

    entry = BibEntry(
        key="k", entry_type=EntryType.ARTICLE, title="Parameter Allocation & Regularization",
        authors=["d'Amore, Francesco"], year=2022, venue="AAAI", ids=Identifiers(doi="10.1/x"),
    )
    rec = SourceRecord(source="crossref", title="Parameter Allocation &amp; Regularization",
                       venue="AAAI")
    art = MatchedArtifact(records=[rec], versions=[rec], best_record=rec)
    title = next(c for c in deterministic_field_checks(entry, art) if c.field == "title")
    assert title.status == "ok" and not title.needs_llm
    assert title.canonical_value == "Parameter Allocation & Regularization"


def test_an_encoded_open_library_title_is_compatible():
    from reference_audit.sources.openlibrary import _title_compatible

    assert _title_compatible("Fluids & Plasmas", "Fluids &amp; Plasmas")
