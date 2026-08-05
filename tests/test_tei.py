"""GROBID TEI → BibEntry / CitationContext mapping. Pure, offline, no GROBID needed.

TEI documents are inline module constants (the idiom `test_web.py` uses for recorded HTML) so each
edge case is readable next to the assertion that pins it. The realistic end-to-end fixture lives in
`tests/fixtures/tei/` and is exercised by `test_pdf_extraction.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reference_audit.models import EntryType
from reference_audit.pdf.grobid import GROBID_IMAGE
from reference_audit.pdf.tei import TeiParseError, parse_tei


def _tei(back: str = "", body: str = "") -> str:
    return (
        '<TEI xmlns="http://www.tei-c.org/ns/1.0" xmlns:xml="http://www.w3.org/XML/1998/namespace">'
        f"<text><body>{body}</body><back>{back}</back></text></TEI>"
    )


def _listbibl(*structs: str) -> str:
    return f'<div type="references"><listBibl>{"".join(structs)}</listBibl></div>'


_ARTICLE = """
<biblStruct xml:id="b0">
  <analytic>
    <title level="a" type="main">Multiscale structural complexity of natural patterns</title>
    <author><persName><forename type="first">Andrey</forename><surname>Bagrov</surname></persName></author>
    <author><persName><forename type="first">Ilia</forename><surname>Iakovlev</surname></persName></author>
    <idno type="DOI">https://doi.org/10.1073/PNAS.2004976117</idno>
  </analytic>
  <monogr>
    <title level="j">Proceedings of the National Academy of Sciences</title>
    <imprint>
      <biblScope unit="volume">117</biblScope>
      <biblScope unit="issue">48</biblScope>
      <biblScope unit="page" from="30241" to="30251"/>
      <date type="published" when="2020-11-16">2020</date>
    </imprint>
  </monogr>
</biblStruct>
"""

# A book chapter with an EMPTY <analytic> title — the case where a series name would be mistaken for
# the paper title if monogr/@level were ignored.
_CHAPTER = """
<biblStruct xml:id="b1">
  <analytic><title level="a" type="main">Emergent phenomena in living matter</title></analytic>
  <monogr>
    <title level="m">Lecture Notes in Physics</title>
    <imprint>
      <publisher>Springer</publisher>
      <biblScope unit="page">55</biblScope>
      <date when="2011"/>
    </imprint>
    <idno type="ISBN">978-3-540-74252-7</idno>
  </monogr>
</biblStruct>
"""

_PROCEEDINGS = """
<biblStruct xml:id="b2">
  <analytic><title level="a">Lenia and expanded universe</title></analytic>
  <monogr>
    <title level="m">Proceedings of the Artificial Life Conference</title>
    <meeting><address><addrLine>Tokyo</addrLine></address></meeting>
    <imprint><date when="2019"/></imprint>
  </monogr>
</biblStruct>
"""

_BOOK = """
<biblStruct xml:id="b3">
  <monogr>
    <title level="m">Fitness Landscapes and the Origin of Species</title>
    <author><persName><surname>Gavrilets</surname></persName></author>
    <imprint>
      <publisher>Princeton University Press</publisher>
      <date when="2004"/>
    </imprint>
    <idno type="ISBN">9780691119830</idno>
  </monogr>
</biblStruct>
"""

_ARXIV_ONLY = """
<biblStruct xml:id="b4">
  <analytic>
    <title level="a">Towards a theory of machine learning</title>
    <author><persName><surname>Vanchurin</surname></persName></author>
    <idno type="arXiv">arXiv:2004.09280</idno>
  </analytic>
  <monogr><imprint><date when="2022"/></imprint></monogr>
</biblStruct>
"""

_WEB_MISC = """
<biblStruct xml:id="b5">
  <analytic>
    <title level="a">Sustainable Development Misconception Study 2020</title>
    <ptr type="url" target="https://www.gapminder.org/ignorance/studies/sdg2020/"/>
  </analytic>
  <monogr><imprint><date when="2020"/></imprint></monogr>
</biblStruct>
"""

# GROBID recognized a reference but recovered nothing usable from it: no title, no identifier, only
# the raw string. This is the record that MUST be reported as uncheckable rather than searched on.
_RAW_ONLY = """
<biblStruct xml:id="b6">
  <monogr><imprint><date when="1998"/></imprint></monogr>
  <note type="raw_reference">R. Laughlin et al., unreadable scan, 1998.</note>
</biblStruct>
"""

_NO_YEAR = """
<biblStruct xml:id="b7">
  <analytic>
    <title level="a">A reference with no date at all</title>
    <author><persName><surname>Nemo</surname></persName></author>
  </analytic>
  <monogr><title level="j">Journal of Undated Things</title></monogr>
</biblStruct>
"""

# Two biblStructs declaring the SAME xml:id — must not silently merge into one entry.
_DUP_IDS = """
<biblStruct xml:id="b0"><analytic><title level="a">First</title></analytic></biblStruct>
<biblStruct xml:id="b0"><analytic><title level="a">Second</title></analytic></biblStruct>
"""

_ORG_AUTHOR = """
<biblStruct xml:id="b8">
  <analytic>
    <title level="a">World Development Report</title>
    <author><persName><surname>Smith and Sons</surname></persName></author>
  </analytic>
  <monogr><imprint><date when="2015"/></imprint></monogr>
</biblStruct>
"""


# ---------------------------------------------------------------- reference mapping


def test_article_maps_every_field():
    refs = parse_tei(_tei(back=_listbibl(_ARTICLE)))
    (entry,) = refs.entries
    assert entry.key == "b0"
    assert entry.entry_type == EntryType.ARTICLE
    assert entry.title == "Multiscale structural complexity of natural patterns"
    # Rendered in BibTeX's "Surname, Forename" order — see `_person_name`: the content hash is taken
    # over the author strings as written, so both front ends must agree on the convention.
    assert entry.authors == ["Bagrov, Andrey", "Iakovlev, Ilia"]
    assert entry.year == 2020
    assert entry.venue == "Proceedings of the National Academy of Sciences"
    assert entry.pages == "30241--30251"
    assert entry.raw_fields["volume"] == "117"
    assert entry.raw_fields["number"] == "48"
    # DOI arrives from GROBID in URL form and is stored bare, so the ".bib-style" advice about URL
    # DOIs cannot fire on a PDF-derived entry.
    assert entry.ids.doi == "10.1073/pnas.2004976117"
    assert "doi.org" not in entry.raw_fields["doi"]
    assert refs.extraction_failures == {}


def test_monogr_level_m_is_a_venue_not_the_title():
    """A `level="m"` series name must never become the paper's title."""
    refs = parse_tei(_tei(back=_listbibl(_CHAPTER)))
    (entry,) = refs.entries
    assert entry.title == "Emergent phenomena in living matter"
    assert entry.venue == "Lecture Notes in Physics"
    assert entry.entry_type == EntryType.INCOLLECTION
    assert entry.ids.isbn13 == "9783540742527"


@pytest.mark.parametrize(
    "struct, expected",
    [
        (_ARTICLE, EntryType.ARTICLE),
        (_PROCEEDINGS, EntryType.INPROCEEDINGS),
        (_BOOK, EntryType.BOOK),
        (_CHAPTER, EntryType.INCOLLECTION),
        (_WEB_MISC, EntryType.MISC),
        (_RAW_ONLY, EntryType.UNKNOWN),
    ],
)
def test_entry_type_inference(struct, expected):
    refs = parse_tei(_tei(back=_listbibl(struct)))
    assert refs.entries[0].entry_type == expected


def test_arxiv_idno_becomes_an_identifier():
    refs = parse_tei(_tei(back=_listbibl(_ARXIV_ONLY)))
    (entry,) = refs.entries
    assert entry.ids.arxiv_id == "2004.09280"
    assert entry.ids.doi is None


def test_url_ptr_becomes_the_url_identifier():
    refs = parse_tei(_tei(back=_listbibl(_WEB_MISC)))
    (entry,) = refs.entries
    assert entry.ids.url == "https://www.gapminder.org/ignorance/studies/sdg2020/"


def test_organizational_author_is_not_split_on_and():
    """The `authors=` seam exists for exactly this: TEI gives structured names, so no re-split."""
    refs = parse_tei(_tei(back=_listbibl(_ORG_AUTHOR)))
    assert refs.entries[0].authors == ["Smith and Sons"]


def test_author_with_no_surname_element_keeps_document_order():
    """Nothing to key the reordering on, so the parts are joined as they appear — not guessed at."""
    struct = (
        '<biblStruct xml:id="b9"><analytic><title level="a">T</title>'
        "<author><persName><forename>Cher</forename></persName></author>"
        "</analytic></biblStruct>"
    )
    refs = parse_tei(_tei(back=_listbibl(struct)))
    assert refs.entries[0].authors == ["Cher"]


def test_year_falls_back_to_an_undated_date_element():
    refs = parse_tei(_tei(back=_listbibl(_CHAPTER)))
    assert refs.entries[0].year == 2011


def test_missing_year_is_left_none_not_guessed():
    refs = parse_tei(_tei(back=_listbibl(_NO_YEAR)))
    assert refs.entries[0].year is None


def test_duplicate_xml_ids_do_not_merge_entries():
    refs = parse_tei(_tei(back=_listbibl(_DUP_IDS)))
    assert [e.key for e in refs.entries] == ["b0", "b0-2"]
    assert [e.title for e in refs.entries] == ["First", "Second"]


def test_biblstruct_without_xml_id_falls_back_to_position():
    refs = parse_tei(
        _tei(back=_listbibl('<biblStruct><analytic><title level="a">Anon</title></analytic></biblStruct>'))
    )
    assert refs.entries[0].key == "b0"


def test_printed_reference_number_is_recorded():
    refs = parse_tei(_tei(back=_listbibl(_ARTICLE, _CHAPTER, _BOOK)))
    assert [e.raw_fields["grobid_ref_index"] for e in refs.entries] == ["1", "2", "3"]


# ---------------------------------------------------------------- per-record extraction failures


def test_title_less_reference_reports_why_and_keeps_the_raw_string():
    refs = parse_tei(_tei(back=_listbibl(_RAW_ONLY)))
    (entry,) = refs.entries
    assert entry.title == ""
    assert not entry.ids.any_present()
    reasons = refs.extraction_failures["b6"]
    assert any("no title" in r for r in reasons)
    # The raw reference is quoted so a human can find it in the PDF.
    assert any("unreadable scan" in r for r in reasons)
    assert entry.raw_fields["grobid_raw"].startswith("R. Laughlin")


def test_author_less_reference_is_reported_but_still_audited():
    refs = parse_tei(_tei(back=_listbibl(_WEB_MISC)))
    (entry,) = refs.entries
    assert entry.title  # still usable
    assert any("no author list" in r for r in refs.extraction_failures["b5"])


def test_empty_bibliography_yields_no_entries_rather_than_an_error():
    """"GROBID found nothing" is a legitimate result the caller must classify — not an exception."""
    refs = parse_tei(_tei(back=_listbibl()))
    assert refs.entries == []
    assert refs.markers_seen == 0


# ---------------------------------------------------------------- citation contexts


_BODY_TWO_CITES = (
    "<div><head>Introduction</head>"
    "<p>Complexity has many definitions. "
    'Structural complexity is set by scaling properties <ref type="bibr" target="#b0">[1]</ref>. '
    'A later claim rests on two sources <ref type="bibr" target="#b0">[1]</ref>'
    '<ref type="bibr" target="#b1">[2]</ref>.</p>"'
    "</div>"
)


def test_contexts_are_sentence_granular_and_ordinal_ordered():
    refs = parse_tei(_tei(back=_listbibl(_ARTICLE, _CHAPTER), body=_BODY_TWO_CITES))
    b0 = refs.contexts["b0"]
    assert [c.ordinal for c in b0] == [0, 1]
    assert b0[0].text == "Structural complexity is set by scaling properties ."
    assert b0[1].text.startswith("A later claim rests on two sources")
    assert all(c.command == "bibr" for c in b0)


def test_marker_surface_text_is_dropped_from_the_context():
    """`[1]` is not part of the citing claim, exactly as `\\cite{...}` is not."""
    refs = parse_tei(_tei(back=_listbibl(_ARTICLE), body=_BODY_TWO_CITES))
    assert "[1]" not in refs.contexts["b0"][0].text


def test_two_keys_in_one_bracket_share_the_sentence():
    refs = parse_tei(_tei(back=_listbibl(_ARTICLE, _CHAPTER), body=_BODY_TWO_CITES))
    assert refs.contexts["b1"][0].text == refs.contexts["b0"][1].text


def test_short_context_folds_in_the_preceding_sentence():
    body = (
        "<div><p>Frustration is the main cause of complexity in physical systems. "
        'See <ref type="bibr" target="#b0">[1]</ref>.</p></div>'
    )
    refs = parse_tei(_tei(back=_listbibl(_ARTICLE), body=body))
    text = refs.contexts["b0"][0].text
    assert "Frustration is the main cause" in text, "a bare trailing cite must gain its claim"


def test_unlinked_marker_is_counted_never_guessed():
    body = '<div><p>An unattributed claim about complexity <ref type="bibr">[9]</ref>.</p></div>'
    refs = parse_tei(_tei(back=_listbibl(_ARTICLE), body=body))
    assert refs.unresolved_markers == 1
    assert refs.markers_seen == 1
    assert refs.contexts == {}


def test_dangling_target_is_reported_not_dropped():
    body = '<div><p>A claim pointing at a missing entry <ref type="bibr" target="#b99">[99]</ref>.</p></div>'
    refs = parse_tei(_tei(back=_listbibl(_ARTICLE), body=body))
    assert refs.dangling_targets == ["b99"]
    assert refs.contexts == {}


def test_markers_in_nested_divs_are_counted_once():
    """Guards the `div.findall(p)` choice: `div.iter(ref)` would double-count and inflate ordinals."""
    body = (
        "<div><head>Outer</head><p>An outer claim about scaling behaviour in nature.</p>"
        '<div><head>Inner</head><p>An inner claim about scaling <ref type="bibr" target="#b0">[1]</ref>.</p></div>'
        "</div>"
    )
    refs = parse_tei(_tei(back=_listbibl(_ARTICLE), body=body))
    assert refs.markers_seen == 1
    assert len(refs.contexts["b0"]) == 1


def test_bibliography_and_abstract_markers_are_not_body_citations():
    tei = (
        '<TEI xmlns="http://www.tei-c.org/ns/1.0" xmlns:xml="http://www.w3.org/XML/1998/namespace">'
        "<teiHeader><profileDesc><abstract>"
        '<p>Prior work is extensive <ref type="bibr" target="#b0">[1]</ref>.</p>'
        "</abstract></profileDesc></teiHeader>"
        f"<text><body><p>The body cites nothing here.</p></body><back>{_listbibl(_ARTICLE)}</back></text>"
        "</TEI>"
    )
    refs = parse_tei(tei)
    assert refs.markers_seen == 0
    assert refs.contexts == {}


def test_zero_markers_is_distinguishable_from_zero_citations():
    """`markers_seen == 0` is what lets the caller report citedness as unknown instead of 'uncited'."""
    refs = parse_tei(_tei(back=_listbibl(_ARTICLE), body="<div><p>No citations at all.</p></div>"))
    assert refs.markers_seen == 0
    assert refs.linked_keys() == set()


# ---------------------------------------------------------------- malformed input


def test_truncated_xml_raises_rather_than_returning_a_partial_list():
    truncated = _tei(back=_listbibl(_ARTICLE, _CHAPTER))[: -len("</TEI>") - 40]
    with pytest.raises(TeiParseError):
        parse_tei(truncated)


def test_empty_input_raises():
    with pytest.raises(TeiParseError):
        parse_tei("")


# ------------------------------------------------------- the committed real-world TEI (still offline)

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tei" / "directing-open-ended-evolution"
_PILOT_TEI = _FIXTURE_DIR / "initial.tei.xml"
_PILOT_PROVENANCE = _FIXTURE_DIR / "initial.provenance.txt"


def test_committed_fixture_records_its_provenance():
    """A stale or hand-edited fixture must fail here — offline and immediately.

    The recorded TEI is only meaningful for the GROBID build that produced it, so the pinned image tag
    is asserted rather than trusted. (`tests/pdf_fixtures.py --check` compares content against a live
    service; this catches the cheaper mistake of the files drifting from the constant.)
    """
    provenance = _PILOT_PROVENANCE.read_text(encoding="utf-8")
    assert "Do not edit either file by hand" in provenance
    assert GROBID_IMAGE in provenance
    assert "consolidateCitations=0" in provenance, "the extraction contract must be recorded"
    assert "pdf_fixtures.py --slug" in provenance, "the regeneration command must be pasteable"
    assert "placeholders" in provenance, "the figure substitution must be disclosed"
    # The TEI itself is byte-identical to GROBID's response, so any diff of it is a real change.
    assert _PILOT_TEI.read_text(encoding="utf-8").startswith("<?xml")


def test_real_tei_maps_the_whole_pilot_bibliography():
    """The mapper against real GROBID output, with no GROBID needed to run it.

    Fidelity against the ground-truth `.bib` is measured by `test_pdf_extraction.py` (which needs
    LaTeX + GROBID); this pins that the *mapping* of that recorded output stays correct.
    """
    refs = parse_tei(_PILOT_TEI.read_text(encoding="utf-8"))
    assert len(refs.entries) == 28
    assert all(e.title for e in refs.entries), "every pilot reference has a recoverable title"
    assert all(e.authors for e in refs.entries)
    assert refs.extraction_failures == {}
    assert refs.dangling_targets == []
    assert refs.unresolved_markers == 0
    assert refs.markers_seen == 63
    # Every reference is cited in the text, so every one should carry at least one context.
    assert len(refs.linked_keys()) == 28
    assert all(c.text and c.command == "bibr" for g in refs.contexts.values() for c in g)


def test_real_tei_recovers_a_known_reference_in_full():
    """One reference checked field by field, as a readable anchor for the aggregate counts above."""
    refs = parse_tei(_PILOT_TEI.read_text(encoding="utf-8"))
    (bagrov,) = [
        e for e in refs.entries if e.title == "Multiscale structural complexity of natural patterns"
    ]
    assert bagrov.entry_type == EntryType.ARTICLE
    assert bagrov.venue.startswith("Proceedings of the National Academy")
    assert bagrov.year == 2020
    assert "Bagrov" in bagrov.authors[0]
    assert bagrov.raw_fields["grobid_ref_index"].isdigit()


def test_real_tei_keeps_two_similarly_titled_papers_distinct():
    """The pilot cites both "…of natural patterns" and "…as a quantitative measure of visual
    complexity", which share a 4-word prefix and an author. Fusing them would silently halve the
    bibliography, so the mapper must keep them as separate entries with their own venues and years."""
    refs = parse_tei(_PILOT_TEI.read_text(encoding="utf-8"))
    similar = [e for e in refs.entries if "ultiscale structural complexity" in e.title]
    assert len(similar) == 2
    assert {e.venue for e in similar} == {"Proceedings of the National Academy of Sciences", "Perception"}
    assert len({e.content_hash for e in similar}) == 2
