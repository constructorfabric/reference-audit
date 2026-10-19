"""End-to-end audit of a PDF whose references an LLM reads, with both models scripted. No network.

Covers what exists only once the reading is wired into the pipeline: escalation of an unusable list
to the second model, the document-level failures, the DOI checks before the audit, the re-read of
entries the audit flags, and the extraction cache.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter

from reference_audit.cache.store import AuditCache
from reference_audit.config import AuditConfig
from reference_audit.llm.client import LLMError
from reference_audit.models import (
    BibEntry,
    EntryAudit,
    EntryType,
    FieldFinding,
    Identifiers,
    SourceQueryResult,
    SourceRecord,
    Verdict,
)
from reference_audit.pdf.llm_refs import PdfReadError, PrintedReferenceList
from reference_audit.pipeline import AuditPipeline, EmptyBibliographyError, _reread_reason
from reference_audit.report import render_text
from reference_audit.sources.base import SourceAdapter
from test_llm_refs import reading, ref

FIRST, SECOND = "first-model", "second-model"

KAPLAN = ref("Scaling laws for neural language models", ("J. Kaplan", "S. McCandlish"))
VASWANI = ref("Attention is all you need", ("A. Vaswani",), year=2017)
# The same printed reference read wrongly: a title word dropped and the year mangled.
VASWANI_MISREAD = ref("Attention is all you", ("A. Vaswani",), year=2071,
                      raw=VASWANI.raw_text)


class ScriptedReader:
    """Stands in for `LLMClient`: each model returns its scripted readings (or errors) in order."""

    def __init__(self, **by_model: list[PrintedReferenceList | Exception]):
        self.script = {FIRST: by_model.get("first", []), SECOND: by_model.get("second", [])}
        self.calls: list[str] = []

    async def read_pdf(self, pdf_path, *, model, instructions, prompt, schema_model, timeout=0):
        self.calls.append(model)
        if not self.script[model]:
            raise AssertionError(f"{model} was asked to read more often than scripted")
        out = self.script[model].pop(0)
        if isinstance(out, Exception):
            raise out
        return out, {"input_tokens": 10, "output_tokens": 5}

    async def structured(self, *args, **kwargs):
        raise AssertionError("no adjudication is expected in these tests")

    async def aclose(self) -> None:
        pass


class KnownTitles(SourceAdapter):
    """Finds exactly the works whose titles it knows; records every metadata query."""

    name = "crossref"
    handles = {EntryType.ARTICLE, EntryType.MISC, EntryType.UNKNOWN, EntryType.INPROCEEDINGS}
    rate_per_sec = 100.0

    def __init__(self, *known: SourceRecord):
        self.known = {r.title.lower(): r for r in known}
        self.queried: list[str] = []

    async def lookup_by_id(self, ids):
        self.queried.append(ids.doi or "?")
        return SourceQueryResult(source=self.name, query_kind="id", records=[])

    async def search_by_metadata(self, entry, limit: int = 10):
        self.queried.append(entry.title)
        hit = self.known.get(entry.title.lower())
        return SourceQueryResult(source=self.name, query_kind="metadata",
                                 records=[hit] if hit else [])

    async def aclose(self) -> None:
        pass


def _record(r) -> SourceRecord:
    return SourceRecord(source="crossref", source_native_id=r.title, title=r.title,
                        authors=list(r.authors), year=r.year, venue=r.venue)


@pytest.fixture
def pdf(tmp_path: Path, monkeypatch) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    path = tmp_path / "paper.pdf"
    with path.open("wb") as f:
        writer.write(f)
    # The blank page has no text; each test that needs a text layer sets `TEXT_LAYER` below.
    monkeypatch.setattr("reference_audit.pipeline.pdf_text_layer", lambda p: TEXT_LAYER["text"])
    TEXT_LAYER["text"] = ""
    return path


TEXT_LAYER = {"text": ""}


def _config(**kw) -> AuditConfig:
    return AuditConfig(pdf_extraction_model=FIRST, pdf_escalation_model=SECOND, check_fields=False,
                       **kw)


async def _run(pdf: Path, reader: ScriptedReader, adapter: SourceAdapter,
               cache: AuditCache | None = None, **config):
    pipeline = AuditPipeline(_config(**config), adapters=[adapter], llm=reader, cache=cache)
    try:
        report = await pipeline.run_pdf(pdf)
    finally:
        await pipeline.aclose()
    # An entry whose audit crashed is left unresolved with an issue, which assertions on other
    # issues would not notice: fail loudly instead, so a stub mismatch cannot pass as a result.
    crashed = [i for a in report.entries for i in a.issues if i.startswith("audit failed")]
    assert not crashed, crashed
    return report


# ------------------------------------------------------------------------------- the list


async def test_a_usable_first_reading_is_audited_without_the_second_model(pdf):
    reader = ScriptedReader(first=[reading(KAPLAN, VASWANI)])
    adapter = KnownTitles(_record(KAPLAN), _record(VASWANI))
    report = await _run(pdf, reader, adapter)
    assert reader.calls == [FIRST]
    assert [a.verdict.kind for a in report.entries] == ["exactly_one", "exactly_one"]
    assert report.input_kind == "pdf" and report.citation_linking == "unavailable"
    assert any("read from paper.pdf by a language model" in n for n in report.notes)
    assert report.summary["pdf_reading"]["list_read_by"] == FIRST
    assert report.summary["pdf_reading"]["entries_reread"] == 0
    assert "citedness unknown" in render_text(report)


async def test_a_truncated_first_reading_is_replaced_by_the_second_models(pdf):
    cut_short = reading(KAPLAN, anchor=VASWANI.raw_text[:60])
    reader = ScriptedReader(first=[cut_short], second=[reading(KAPLAN, VASWANI)])
    report = await _run(pdf, reader, KnownTitles(_record(KAPLAN), _record(VASWANI)))
    assert reader.calls == [FIRST, SECOND]
    assert len(report.entries) == 2
    assert {a.entry.raw_fields["pdf_read_by"] for a in report.entries} == {SECOND}
    assert any(f"{FIRST}'s reading of the reference list was not used" in n for n in report.notes)


async def test_when_neither_reading_is_usable_the_document_fails_with_both_reasons(pdf):
    adapter = KnownTitles()
    reader = ScriptedReader(first=[reading(KAPLAN, coverage="stopped_early")],
                            second=[LLMError("second-model refused to read paper.pdf: no")])
    with pytest.raises(PdfReadError) as exc:
        await _run(pdf, reader, adapter)
    assert "stopped before the end" in str(exc.value) and "refused" in str(exc.value)
    assert adapter.queried == [], "nothing is audited from a partial list"


async def test_no_reference_list_is_confirmed_by_the_second_model(pdf):
    reader = ScriptedReader(first=[reading()], second=[reading()])
    with pytest.raises(EmptyBibliographyError, match="found none either"):
        await _run(pdf, reader, KnownTitles())


async def test_a_list_the_first_model_missed_is_found_by_the_second(pdf):
    reader = ScriptedReader(first=[reading()], second=[reading(KAPLAN)])
    report = await _run(pdf, reader, KnownTitles(_record(KAPLAN)))
    assert [a.verdict.kind for a in report.entries] == ["exactly_one"]
    assert any("it found no reference list" in n for n in report.notes)


async def test_without_the_openai_api_the_reading_fails_with_the_alternatives_named(pdf):
    pipeline = AuditPipeline(_config(use_llm=False), adapters=[KnownTitles()])
    try:
        with pytest.raises(PdfReadError, match="--pdf-extractor grobid"):
            await pipeline.run_pdf(pdf)
    finally:
        await pipeline.aclose()


# ------------------------------------------------------------------------------- DOI checks


async def test_a_doi_absent_from_the_page_is_re_read_before_the_audit(pdf):
    TEXT_LAYER["text"] = ("A. Vaswani. Attention is all you need. doi:10.5555/3295222.3295349. "
                          + "filler " * 40)
    invented = ref(VASWANI.title, VASWANI.authors, year=2017, doi="10.1016/j.amepre.2013.01.006",
                   raw=VASWANI.raw_text)
    printed = ref(VASWANI.title, VASWANI.authors, year=2017, doi="10.5555/3295222.3295349",
                  raw=VASWANI.raw_text)
    adapter = KnownTitles(_record(VASWANI).model_copy(
        update={"ids": Identifiers(doi="10.5555/3295222.3295349")}))
    reader = ScriptedReader(first=[reading(invented)], second=[reading(printed)])
    report = await _run(pdf, reader, adapter)
    (audit,) = report.entries
    assert audit.entry.ids.doi == "10.5555/3295222.3295349"
    assert audit.entry.raw_fields["pdf_read_by"] == SECOND
    assert "10.1016/j.amepre.2013.01.006" not in adapter.queried, "the invented DOI is never used"
    assert audit.entry.raw_fields["pdf_reread"].startswith(
        "re-read with second-model because the DOI 10.1016/j.amepre.2013.01.006 does not occur")
    assert audit.verdict is not None and audit.verdict.kind == "exactly_one"
    assert not any("re-read" in i for i in audit.issues), "a replacing re-read is a reading note"
    assert reader.calls == [FIRST, SECOND]


async def test_a_doi_both_models_read_on_two_titles_stands_as_printed(pdf):
    a = ref("Facebook use predicts declines in well-being", doi="10.1016/j.amepre.2017.01.010")
    b = ref("Social media use and perceived social isolation", doi="10.1016/j.amepre.2017.01.010")
    reader = ScriptedReader(first=[reading(a, b)], second=[reading(a, b)])
    report = await _run(pdf, reader, KnownTitles())
    for audit in report.entries:
        assert audit.entry.raw_fields["pdf_read_by"] == FIRST
        assert any("carried by references #1, #2" in i for i in audit.issues)
        assert any("same kind of problem" in i for i in audit.issues)
    assert reader.calls == [FIRST, SECOND], "one second reading serves both entries"


# ------------------------------------------------------------------------------- re-reads


async def test_an_entry_the_audit_flags_is_re_read_and_audited_again_if_read_differently(pdf):
    adapter = KnownTitles(_record(KAPLAN), _record(VASWANI))
    reader = ScriptedReader(first=[reading(KAPLAN, VASWANI_MISREAD)],
                            second=[reading(KAPLAN, VASWANI)])
    report = await _run(pdf, reader, adapter)
    kaplan, vaswani = report.entries
    assert kaplan.entry.raw_fields["pdf_read_by"] == FIRST, "a matched entry is left alone"
    assert vaswani.entry.raw_fields["pdf_read_by"] == SECOND
    assert vaswani.entry.key == "r2" and vaswani.entry.raw_fields["pdf_ref_index"] == "2"
    assert vaswani.verdict is not None and vaswani.verdict.kind == "exactly_one"
    note = vaswani.entry.raw_fields["pdf_reread"]
    assert note.startswith("re-read with second-model after a 'no match' verdict")
    assert "year: first-model read 2071" in note
    text = render_text(report)
    assert "    · " + note in text
    assert report.summary["verdicts"]["exactly_one"] == 2
    assert report.summary["pdf_reading"]["entries_from_escalation_model"] == 1


async def test_a_flagged_entry_read_the_same_way_keeps_its_finding(pdf):
    unknown = ref("A paper no database has", ("N. Body",))
    reader = ScriptedReader(first=[reading(KAPLAN, unknown)], second=[reading(KAPLAN, unknown)])
    report = await _run(pdf, reader, KnownTitles(_record(KAPLAN)))
    flagged = report.entries[1]
    assert flagged.verdict.kind == "none"
    assert any("the same reading, so the finding concerns the reference as printed" in i
               for i in flagged.issues)


async def test_several_flagged_entries_share_one_second_reading(pdf):
    x, y = ref("Unknown one", ("P. Q",)), ref("Unknown two", ("R. S",))
    reader = ScriptedReader(first=[reading(x, y)], second=[reading(x, y)])
    await _run(pdf, reader, KnownTitles())
    assert reader.calls == [FIRST, SECOND]


async def test_a_failed_second_reading_is_reported_on_each_flagged_entry(pdf):
    unknown = ref("A paper no database has", ("N. Body",))
    reader = ScriptedReader(first=[reading(unknown)], second=[LLMError("second-model: timeout")])
    report = await _run(pdf, reader, KnownTitles())
    (audit,) = report.entries
    assert audit.verdict.kind == "none"
    assert any("could not be re-read with second-model" in i and "timeout" in i
               for i in audit.issues)


def _audited(verdict_kind=None, findings=()):
    audit = EntryAudit(entry=BibEntry(key="r1", title="T"), field_findings=list(findings))
    audit.verdict = Verdict(kind=verdict_kind) if verdict_kind else None
    return audit


@pytest.mark.parametrize(
    ("audit", "reason"),
    [
        (_audited("none"), "a 'no match' verdict"),
        (_audited("contradiction"), "a contradiction verdict"),
        (_audited("exactly_one", [FieldFinding(field="year", status="error")]),
         "an error finding on year"),
        (_audited("exactly_one", [FieldFinding(field="pages", status="error")]), None),
        (_audited("exactly_one", [FieldFinding(field="title", status="formatting")]), None),
        (_audited("multiple"), None),
        (_audited(None), None),   # unresolved: a source failed, not a misreading
    ],
)
def test_only_outcomes_a_misreading_can_cause_trigger_a_re_read(audit, reason):
    assert _reread_reason(audit) == reason


async def test_one_pipeline_auditing_two_pdfs_keeps_their_readings_apart(pdf, tmp_path):
    other = tmp_path / "other.pdf"
    other.write_bytes(pdf.read_bytes() + b"\n% another document\n")
    unknown = ref("A paper no database has", ("N. Body",))
    reader = ScriptedReader(first=[reading(KAPLAN), reading(unknown)], second=[reading(unknown)])
    pipeline = AuditPipeline(_config(), adapters=[KnownTitles(_record(KAPLAN))], llm=reader)
    try:
        first = await pipeline.run_pdf(pdf)
        second = await pipeline.run_pdf(other)
    finally:
        await pipeline.aclose()
    assert first.summary["pdf_reading"]["entries_reread"] == 0
    assert second.summary["pdf_reading"]["entries_reread"] == 1
    assert second.summary["pdf_reading"]["usage"] == {
        FIRST: [{"input_tokens": 10, "output_tokens": 5}],
        SECOND: [{"input_tokens": 10, "output_tokens": 5}],
    }


# ------------------------------------------------------------------------------- cache


async def test_a_usable_reading_is_cached_and_a_failed_one_is_not(pdf, tmp_path):
    cache = AuditCache(tmp_path / "cache.db", pipeline_version="test", model="adjudicator")
    try:
        adapter = KnownTitles(_record(KAPLAN))
        first_run = ScriptedReader(first=[reading(KAPLAN)])
        await _run(pdf, first_run, adapter, cache=cache)
        again = ScriptedReader()
        report = await _run(pdf, again, adapter, cache=cache)
        assert again.calls == [], "the stored reading answers the second run"
        assert report.entries[0].entry.raw_fields["pdf_read_by"] == FIRST

        cache_failed = AuditCache(tmp_path / "other.db", pipeline_version="test", model="x")
        try:
            failing = ScriptedReader(first=[reading(KAPLAN, coverage="stopped_early")],
                                     second=[reading(KAPLAN)])
            await _run(pdf, failing, adapter, cache=cache_failed)
            assert cache_failed.get_pdf_extraction(
                __import__("hashlib").sha256(pdf.read_bytes()).hexdigest(), FIRST, "1"
            ) is None
        finally:
            cache_failed.close()
    finally:
        cache.close()


# ------------------------------------------------------------------------------- CLI


def test_cli_rejects_an_unknown_extractor(tmp_path):
    from typer.testing import CliRunner

    from reference_audit import cli

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    result = CliRunner().invoke(cli.app, ["audit", str(pdf), "--pdf-extractor", "ocr"])
    assert result.exit_code != 0 and "--pdf-extractor must be 'llm' or 'grobid'" in result.output
