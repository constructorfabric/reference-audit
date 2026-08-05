"""End-to-end audit of a PDF, with GROBID stubbed by a canned TEI. No container, no network.

Covers the behaviors that only exist once the TEI mapper is wired into the pipeline: the fatal/
per-record failure split, the honest bookkeeping when citation linking is unavailable, alignment
joining contexts to entries by synthetic key, and cache sharing with an equivalent `.bib` entry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reference_audit.cache.store import AuditCache
from reference_audit.config import AuditConfig
from reference_audit.models import EntryType, SourceQueryResult, Verdict
from reference_audit.parsing.bib import parse_bib
from reference_audit.pdf.grobid import GrobidRequestError
from reference_audit.pipeline import (
    AuditPipeline,
    EmptyBibliographyError,
    build_pdf_parse_report,
)
from reference_audit.report import render_json, render_text
from reference_audit.sources.base import SourceAdapter


class StubGrobid:
    """Stands in for `GrobidClient`, returning a canned TEI (or raising) without any HTTP."""

    def __init__(self, tei: str | Exception):
        self.tei = tei
        self.calls: list[Path] = []

    async def fulltext_tei(self, pdf_path) -> str:
        self.calls.append(Path(pdf_path))
        if isinstance(self.tei, Exception):
            raise self.tei
        return self.tei

    async def aclose(self) -> None:
        pass


class RecordingAdapter(SourceAdapter):
    """Finds nothing, but records what it was asked — so "never searched" is assertable.

    Named `crossref` because `route_entry` selects adapters by name from a fixed set; a stub with a
    novel name would simply never be routed, and the test would pass for the wrong reason.
    """

    name = "crossref"
    handles = {
        EntryType.ARTICLE, EntryType.BOOK, EntryType.INPROCEEDINGS,
        EntryType.INCOLLECTION, EntryType.MISC, EntryType.UNKNOWN,
    }
    rate_per_sec = 100.0

    def __init__(self) -> None:
        self.id_queries: list[str] = []
        self.metadata_queries: list[str] = []

    @property
    def queried(self) -> list[str]:
        return self.id_queries + self.metadata_queries

    async def lookup_by_id(self, ids):
        self.id_queries.append(ids.doi or ids.arxiv_id or ids.isbn13 or "?")
        return SourceQueryResult(source=self.name, query_kind="id", records=[])

    async def search_by_metadata(self, entry, limit: int = 10):
        self.metadata_queries.append(entry.key)
        return SourceQueryResult(source=self.name, query_kind="metadata", records=[])

    async def aclose(self) -> None:
        pass


def _tei(structs: str, body: str = "") -> str:
    return (
        '<TEI xmlns="http://www.tei-c.org/ns/1.0" xmlns:xml="http://www.w3.org/XML/1998/namespace">'
        f"<text><body>{body}</body>"
        f'<back><div type="references"><listBibl>{structs}</listBibl></div></back></text></TEI>'
    )


_ONE_ARTICLE = """
<biblStruct xml:id="b0">
  <analytic>
    <title level="a">Multiscale structural complexity of natural patterns</title>
    <author><persName><forename>Andrey</forename><surname>Bagrov</surname></persName></author>
    <idno type="DOI">10.1073/pnas.2004976117</idno>
  </analytic>
  <monogr>
    <title level="j">Proceedings of the National Academy of Sciences</title>
    <imprint><date type="published" when="2020"/></imprint>
  </monogr>
</biblStruct>
"""

# GROBID recognized a reference but recovered no title and no identifier.
_NO_ANCHOR = """
<biblStruct xml:id="b1">
  <monogr><imprint><date when="1998"/></imprint></monogr>
  <note type="raw_reference">R. Laughlin et al., illegible scan, 1998.</note>
</biblStruct>
"""

_BODY_CITING_B0 = (
    "<div><p>Structural complexity is determined by the scaling properties of the system "
    '<ref type="bibr" target="#b0">[1]</ref>.</p></div>'
)


@pytest.fixture
def pdf(tmp_path) -> Path:
    p = tmp_path / "paper.pdf"
    p.write_bytes(b"%PDF-1.7\n")
    return p


# ---------------------------------------------------------------- fatal failures


async def test_no_reference_list_is_a_fatal_empty_bibliography(pdf):
    """Reuses the existing exit-code-2 signal rather than inventing a second 'nothing to audit'."""
    client = StubGrobid(_tei(""))
    with pytest.raises(EmptyBibliographyError, match="no reference list"):
        await build_pdf_parse_report(pdf, client=client)


async def test_malformed_tei_is_reported_as_a_request_error(pdf):
    client = StubGrobid("<TEI><text><body>truncated")
    with pytest.raises(GrobidRequestError):
        await build_pdf_parse_report(pdf, client=client)


# ---------------------------------------------------------------- report shape


async def test_pdf_report_declares_its_provenance_and_inapplicable_fields(pdf):
    parsed = await build_pdf_parse_report(
        pdf, client=StubGrobid(_tei(_ONE_ARTICLE, _BODY_CITING_B0))
    )
    report = parsed.report
    assert report.input_kind == "pdf"
    assert report.citation_linking == "available"
    # Structurally inapplicable for a PDF — empty, and the renderer omits their meaningless counts.
    assert report.commented_twins == []
    assert report.missing_includes == []
    assert report.cited_but_missing == []
    assert any("extracted from paper.pdf by GROBID" in n for n in report.notes)
    assert report.summary["total_entries"] == 1
    assert report.summary["cited"] == 1
    assert report.summary["citation_markers"] == 1


async def test_unlinked_markers_are_reported_in_the_summary(pdf):
    body = '<div><p>An unattributed claim about complexity <ref type="bibr">[9]</ref>.</p></div>'
    parsed = await build_pdf_parse_report(pdf, client=StubGrobid(_tei(_ONE_ARTICLE, body)))
    assert parsed.report.summary["unresolved_citation_markers"] == 1
    assert any("could not be linked" in n for n in parsed.report.notes)


async def test_no_linked_markers_suppresses_uncited_rather_than_asserting_it(pdf):
    """The regression this guards: reporting every reference as 'uncited' when linking simply failed."""
    parsed = await build_pdf_parse_report(pdf, client=StubGrobid(_tei(_ONE_ARTICLE)))
    report = parsed.report
    assert report.citation_linking == "unavailable"
    assert report.uncited == [], "citedness is unknown, so nothing may be claimed as uncited"
    assert report.summary["uncited"] == 0
    assert parsed.contexts_available is False
    assert any("bookkeeping is unavailable" in n for n in report.notes)
    # And the text report must say so rather than printing a confident "0 uncited".
    text = render_text(report)
    assert "citedness unknown" in text


async def test_linked_markers_give_a_real_uncited_list(pdf):
    parsed = await build_pdf_parse_report(
        pdf, client=StubGrobid(_tei(_ONE_ARTICLE + _NO_ANCHOR, _BODY_CITING_B0))
    )
    assert parsed.report.citation_linking == "available"
    assert parsed.report.uncited == ["b1"]


async def test_title_less_reference_is_reported_with_the_raw_text(pdf):
    parsed = await build_pdf_parse_report(pdf, client=StubGrobid(_tei(_NO_ANCHOR)))
    (audit,) = parsed.report.entries
    assert audit.entry.title == ""
    assert any("no title" in i for i in audit.issues)
    assert parsed.report.summary["references_without_title_or_id"] == 1
    # A reader must be able to locate it in the source PDF.
    assert "illegible scan" in render_text(parsed.report)


# ---------------------------------------------------------------- per-record isolation


async def test_anchorless_reference_is_never_searched_on(pdf):
    """A title-less, id-less query would return an arbitrary paper the scorer could accept."""
    adapter = RecordingAdapter()
    pipeline = AuditPipeline(
        AuditConfig(use_llm=False), adapters=[adapter], grobid=StubGrobid(_tei(_NO_ANCHOR))
    )
    try:
        report = await pipeline.run_pdf(pdf)
    finally:
        await pipeline.aclose()
    (audit,) = report.entries
    assert audit.verdict is None, "left explicitly unresolved, not matched to something arbitrary"
    assert any("nothing to search on" in i for i in audit.issues)
    assert adapter.queried == [], "no source may be queried for a reference with no anchor"


async def test_a_usable_reference_is_still_searched_alongside_an_unusable_one(pdf):
    """Record independence: the anchorless entry must not suppress the good one."""
    adapter = RecordingAdapter()
    pipeline = AuditPipeline(
        AuditConfig(use_llm=False),
        adapters=[adapter],
        grobid=StubGrobid(_tei(_ONE_ARTICLE + _NO_ANCHOR)),
    )
    try:
        report = await pipeline.run_pdf(pdf)
    finally:
        await pipeline.aclose()
    assert "b0" in adapter.queried
    assert "b1" not in adapter.queried
    assert len(report.entries) == 2

    # And the two outcomes stay distinguishable, which is the whole point: b0 was searched and nothing
    # matched (a real "no such document" finding), while b1 could not be checked at all. Collapsing
    # them into one bucket would report an extraction failure as a hallucinated reference.
    by_key = {a.entry.key: a for a in report.entries}
    assert by_key["b0"].verdict is not None and by_key["b0"].verdict.kind == "none"
    assert by_key["b1"].verdict is None
    assert report.summary["verdicts"] == {
        "none": 1, "exactly_one": 0, "multiple": 0, "unresolved": 1
    }


# ---------------------------------------------------------------- contexts and keys


async def test_contexts_are_keyed_by_the_same_synthetic_keys_as_the_entries(pdf):
    """This join is what makes --check-citations work on a PDF at all."""
    parsed = await build_pdf_parse_report(
        pdf, client=StubGrobid(_tei(_ONE_ARTICLE, _BODY_CITING_B0))
    )
    entry_keys = {a.entry.key for a in parsed.report.entries}
    assert set(parsed.contexts) <= entry_keys
    assert parsed.contexts["b0"][0].command == "bibr"
    assert "scaling properties" in parsed.contexts["b0"][0].text


async def test_contexts_are_kept_even_when_alignment_is_off(pdf):
    """They come free from the TEI we already fetched; re-deriving would mean re-uploading the PDF."""
    pipeline = AuditPipeline(
        AuditConfig(use_llm=False, check_alignment=False),
        adapters=[RecordingAdapter()],
        grobid=StubGrobid(_tei(_ONE_ARTICLE, _BODY_CITING_B0)),
    )
    try:
        await pipeline.run_pdf(pdf)
        assert "b0" in pipeline._citation_contexts
    finally:
        await pipeline.aclose()


async def test_pdf_and_bib_entries_for_the_same_work_share_a_content_hash(pdf, tmp_path):
    """So a paper audited as a PDF and as a .bib reuses one cached verdict — keys are not hashed."""
    bib = tmp_path / "refs.bib"
    bib.write_text(
        "@article{bagrov2020multiscale,\n"
        "  title = {Multiscale structural complexity of natural patterns},\n"
        "  author = {Bagrov, Andrey},\n"
        "  journal = {Proceedings of the National Academy of Sciences},\n"
        "  year = {2020},\n"
        "  doi = {10.1073/pnas.2004976117}\n}\n",
        encoding="utf-8",
    )
    (from_bib,), _ = parse_bib(bib)
    parsed = await build_pdf_parse_report(pdf, client=StubGrobid(_tei(_ONE_ARTICLE)))
    from_pdf = parsed.report.entries[0].entry

    assert from_pdf.key != from_bib.key, "the PDF has no BibTeX key to recover"
    assert from_pdf.content_hash == from_bib.content_hash


async def test_a_verdict_cached_from_a_bib_run_is_reused_by_a_pdf_run(pdf, tmp_path):
    """The payoff of the shared content hash: the same work is not re-queried per input format."""
    cache = AuditCache(tmp_path / "cache.db", pipeline_version="test", model="test-model")
    adapter = RecordingAdapter()
    try:
        # Seed the cache as a .bib run would, then audit the PDF against the same cache.
        parsed = await build_pdf_parse_report(pdf, client=StubGrobid(_tei(_ONE_ARTICLE)))
        seeded = Verdict(kind="exactly_one", artifacts=[], reason="seeded by a prior .bib run")
        cache.put_entry_verdict(parsed.report.entries[0].entry.content_hash, seeded)

        pipeline = AuditPipeline(
            AuditConfig(use_llm=False, check_fields=False),
            cache=cache,
            adapters=[adapter],
            grobid=StubGrobid(_tei(_ONE_ARTICLE)),
        )
        try:
            report = await pipeline.run_pdf(pdf)
        finally:
            await pipeline.aclose()
    finally:
        cache.close()

    (audit,) = report.entries
    assert audit.from_cache is True
    assert audit.verdict is not None and audit.verdict.kind == "exactly_one"
    assert adapter.queried == [], "the cached verdict must make any source query unnecessary"


# ---------------------------------------------------------------- rendering


async def test_json_report_exposes_the_new_provenance_fields(pdf):
    parsed = await build_pdf_parse_report(
        pdf, client=StubGrobid(_tei(_ONE_ARTICLE, _BODY_CITING_B0))
    )
    payload = render_json(parsed.report)
    assert '"input_kind": "pdf"' in payload
    assert '"citation_linking": "available"' in payload


async def test_text_report_shows_the_printed_reference_number(pdf):
    """`b0` is a GROBID id and means nothing to a reader; the printed number locates it."""
    parsed = await build_pdf_parse_report(
        pdf, client=StubGrobid(_tei(_ONE_ARTICLE, _BODY_CITING_B0))
    )
    text = render_text(parsed.report)
    assert "ref #1" in text
    assert "commented twins" not in text, "a PDF has none — the count would be meaningless"
