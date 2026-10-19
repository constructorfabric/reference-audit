"""Native Word sources, fields and CLI contract; tiny OOXML packages need no Office install."""

import json
import hashlib
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr
from zipfile import ZipFile

import pytest
from typer.testing import CliRunner

from reference_audit.cli import app
from reference_audit.config import AuditConfig
from reference_audit.inputs import DocxInput, InvalidInputError, default_cache_path, resolve_input
from reference_audit.models import EntryType, SourceQueryResult, SourceRecord
from reference_audit.parsing.docx import DocxInputError, parse_docx
from reference_audit.pipeline import AuditPipeline, build_docx_parse_report
from reference_audit.report import render_text
from reference_audit.sources.base import SourceAdapter

B = "http://schemas.openxmlformats.org/officeDocument/2006/bibliography"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
AUTHOR = """<b:Author><b:Author><b:NameList><b:Person>
<b:Last>Schütt</b:Last><b:First>Kristof</b:First><b:Middle>A.</b:Middle>
</b:Person></b:NameList></b:Author></b:Author>"""


def source(tag="one", fields="", kind="JournalArticle", author=AUTHOR):
    return (f"<b:Source><b:Tag>{tag}</b:Tag><b:SourceType>{kind}</b:SourceType>"
            f"<b:Title>A real title</b:Title><b:Year>2021</b:Year>{author}{fields}</b:Source>")


def text(value):
    return f"<w:r><w:t>{escape(value)}</w:t></w:r>"


def simple(instruction):
    return f"<w:fldSimple w:instr={quoteattr(instruction)}>{text('[1]')}</w:fldSimple>"


def complex_field(instruction):
    # Split the instruction across runs, as Word does on save.
    middle = len(instruction) // 2
    return ('<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            f"<w:r><w:instrText>{escape(instruction[:middle])}</w:instrText></w:r>"
            f"<w:r><w:instrText>{escape(instruction[middle:])}</w:instrText></w:r>"
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>' + text("[1]") +
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>')


def package(tmp_path, sources=None, body="", parts=None):
    path = tmp_path / "paper.docx"
    with ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
        if sources is not None:
            z.writestr("customXml/item1.xml", f'<b:Sources xmlns:b="{B}">{sources}</b:Sources>')
        for name, value in (parts or {}).items():
            z.writestr(name, value)
    return path


def test_structured_metadata_and_grouped_split_fields(tmp_path):
    corp = "<b:Author><b:Author><b:Corporate>Smith and Sons</b:Corporate></b:Author></b:Author>"
    path = package(tmp_path, source(fields="<b:DOI>https://doi.org/10.1038/ABC</b:DOI>"
                                  "<b:JournalName>Nature</b:JournalName><b:Issue>7</b:Issue>") +
                   source("two", author=corp) + source("unused"),
                   "<w:p>" + text("Evidence supports this claim ") +
                   complex_field(' CITATION one \\m "two" \\l 1033 \\* MERGEFORMAT ') + text(".") + "</w:p>")
    parsed = parse_docx(path)
    assert parsed.entries[0].authors == ["Schütt, Kristof A."]
    assert parsed.entries[0].ids.doi == "10.1038/abc"
    assert parsed.entries[0].venue == "Nature"
    assert parsed.entries[0].raw_fields["number"] == "7"
    assert parsed.entries[1].authors == ["Smith and Sons"]
    assert parsed.cited_keys == {"one", "two"}
    assert parsed.citation_fields == 1
    assert parsed.contexts["one"][0].text == "Evidence supports this claim ."
    report = build_docx_parse_report(path)
    assert report.uncited == ["unused"]
    assert report.input_kind == "docx"
    assert any("Free-form" in n and "NOT checked" in n for n in report.notes)


@pytest.mark.parametrize(("standard", "kind", "attribute", "expected"), [
    ("arXiv:2511.20976", "Report", "arxiv_id", "2511.20976"),
    ("ISBN 978-0-306-40615-7", "Book", "isbn13", "9780306406157"),
    ("10.1234/5678901234567", "JournalArticle", "doi", "10.1234/5678901234567"),
])
def test_standard_numbers(tmp_path, standard, kind, attribute, expected):
    path = package(tmp_path, source(kind=kind, fields=f"<b:StandardNumber>{standard}</b:StandardNumber>"))
    assert getattr(parse_docx(path).entries[0].ids, attribute) == expected


def test_bad_sources_are_reported_without_losing_neighbors(tmp_path):
    bad = source("broken", author=AUTHOR.replace("<b:Last>Schütt</b:Last>", ""))
    path = package(tmp_path, source() + bad,
                   "<w:p>" + simple("CITATION broken \\m absent") + "</w:p>")
    report = build_docx_parse_report(path)
    assert [a.entry.key for a in report.entries] == ["one"]
    assert [u.key for u in report.unparsed] == ["broken"]
    assert "Last name" in report.unparsed[0].reason
    assert report.cited_but_missing == ["absent"]
    assert "UNPARSEABLE WORD SOURCES" in render_text(report)
    assert "(line 0)" not in render_text(report)


def test_duplicate_tags_and_missing_tags_are_unparsed(tmp_path):
    path = package(tmp_path, source() + source() + source().replace("<b:Tag>one</b:Tag>", ""))
    report = build_docx_parse_report(path)
    assert not report.entries
    assert len(report.unparsed) == 3
    assert "duplicate" in report.unparsed[0].reason
    assert "missing source Tag" in report.unparsed[2].reason


def test_no_native_citations_means_unknown_citedness(tmp_path):
    report = build_docx_parse_report(package(tmp_path, source(), "<w:p>" + text("Typed [1]") + "</w:p>"))
    assert report.citation_linking == "unavailable"
    assert report.uncited == []
    assert "citedness unknown" in render_text(report)


def test_footnotes_simple_fields_and_deleted_citations(tmp_path):
    foot = f'<w:footnotes xmlns:w="{W}"><w:footnote><w:p>{simple("CITATION one")}</w:p></w:footnote></w:footnotes>'
    path = package(tmp_path, source(), "<w:p><w:del>" + simple("CITATION deleted") + "</w:del></w:p>",
                   {"word/footnotes.xml": foot})
    parsed = parse_docx(path)
    assert parsed.cited_keys == {"one"}
    assert parsed.contexts == {}
    assert "no surrounding prose" in parsed.context_failures["one"][0]


def test_bibliography_spanning_paragraphs_is_not_citing_context(tmp_path):
    bibliography = complex_field("BIBLIOGRAPHY")
    bibliography = bibliography.replace(text("[1]"), "</w:p><w:p>" + simple("CITATION fake") + text("Reference text"))
    path = package(tmp_path, source(), "<w:p>" + bibliography + "</w:p><w:p>" +
                   text("The result is supported ") + simple("CITATION one") + text(".") + "</w:p>")
    parsed = parse_docx(path)
    assert parsed.cited_keys == {"one"}
    assert "Reference text" not in parsed.contexts["one"][0].text


@pytest.mark.parametrize("body", [
    '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r></w:p>',
    '<w:p><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>',
    "<w:p>" + simple("CITATION one \\m") + "</w:p>",
])
def test_malformed_fields_fail_explicitly(tmp_path, body):
    with pytest.raises(DocxInputError, match="field"):
        parse_docx(package(tmp_path, source(), body))


def test_bad_xml_part_fails_explicitly(tmp_path):
    path = package(tmp_path, source(), parts={"customXml/item2.xml": "<broken"})
    with pytest.raises(DocxInputError, match="item2.xml"):
        parse_docx(path)


def test_free_form_only_is_rejected_with_warning_and_actionable_error(tmp_path):
    path = package(tmp_path, body="<w:p>" + text("References: [1] Typed reference") + "</w:p>")
    result = CliRunner().invoke(app, ["audit", str(path), "--no-network", "--format", "json"])
    assert result.exit_code == 2
    assert "WARNING: WORD AUDITS CHECK ONLY" in result.stderr
    assert "No structured Word bibliography" in result.stderr
    assert result.stdout == ""


def test_cli_json_warning_is_on_stderr_and_scope_is_in_report(tmp_path):
    path = package(tmp_path, source())
    result = CliRunner().invoke(app, ["audit", str(path), "--no-network", "--format", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["input_kind"] == "docx"
    assert "=" * 86 in result.stderr
    assert "Free-form / manually typed references are NOT checked" in result.stderr
    assert any("NOT checked" in n for n in data["notes"])


def test_word_input_dispatch_and_cache_anchor():
    source_input = resolve_input(Path("papers/Paper.DOCX"), None)
    assert source_input == DocxInput(Path("papers/Paper.DOCX"))
    assert default_cache_path(source_input) == Path("papers/.reference_audit/cache.db")
    for a, b in [("paper.docx", "refs.bib"), ("main.tex", "paper.docx")]:
        with pytest.raises(InvalidInputError, match="whole input"):
            resolve_input(Path(a), Path(b))


async def test_word_network_path_reuses_per_entry_pipeline_and_citing_context(tmp_path):
    path = package(tmp_path, source() + source("two"),
                   "<w:p>" + text("This is a supported statement ") + simple("CITATION one") + text(".") + "</w:p>")
    pipeline = AuditPipeline(config=AuditConfig(use_llm=False), adapters=[])
    seen = []

    async def audit_one(audit):
        seen.append(audit.entry.key)
        audit.unresolved_reasons = ["stub source unavailable"]

    pipeline._audit_entry = audit_one
    try:
        report = await pipeline.run_docx(path)
        assert set(seen) == {"one", "two"}
        assert report.summary["verdicts"]["unresolved"] == 2
        assert pipeline._citation_contexts["one"][0].command == "CITATION"
    finally:
        await pipeline.aclose()


async def test_word_source_failure_does_not_prevent_other_verdicts(tmp_path):
    class WordAdapter(SourceAdapter):
        name = "crossref"
        handles = {EntryType.ARTICLE}

        async def search_by_metadata(self, entry, limit=10):
            if entry.key == "two":
                return SourceQueryResult(source=self.name, query_kind="metadata", error="service unavailable")
            return SourceQueryResult(source=self.name, query_kind="metadata", records=[
                SourceRecord(source=self.name, title=entry.title, authors=entry.authors, year=entry.year),
            ])

    path = package(tmp_path, source() + source("two"))
    pipeline = AuditPipeline(config=AuditConfig(use_llm=False), adapters=[WordAdapter()])
    try:
        report = await pipeline.run_docx(path)
    finally:
        await pipeline.aclose()
    one, two = report.entries
    assert one.verdict is not None and one.verdict.kind == "exactly_one"
    assert two.verdict is None
    assert any("service unavailable" in reason for reason in two.unresolved_reasons)


def test_ai4x_word_template_regression():
    """The actual Office-edited AI4X template, independent of the developer's checkout."""
    path = Path(__file__).parent / "fixtures" / "docx" / "AI4X-2027.docx"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        "58087b020fb7fcc82e8dea23416bfd324eea1c808c6fe31800f32ac99ab575e1"
    )
    parsed = parse_docx(path)
    entries = {entry.key: entry for entry in parsed.entries}
    expected = {
        "AI4X_Jumper2021", "AI4X_Schutt2017", "AI4X_Xie2018", "AI4X_Merchant2023",
        "AI4X_Burger2020_Complete", "AI4X_Roadmap2025", "AI4X_Committee2028",
        "AI4X_Template2026", "AI4X_IAC2025",
    }
    assert set(entries) == parsed.cited_keys == expected
    assert parsed.unparsed == []
    assert parsed.citation_fields == 10
    assert sum(map(len, parsed.contexts.values())) == 11
    assert len(entries["AI4X_Jumper2021"].authors) == 34
    assert len(entries["AI4X_Roadmap2025"].authors) == 68
    assert len(entries["AI4X_Burger2020_Complete"].authors) == 14
    assert entries["AI4X_Committee2028"].authors == ["AI4X Program Committee"]
    assert entries["AI4X_Committee2028"].year == 2028
    assert entries["AI4X_Roadmap2025"].ids.arxiv_id == "2511.20976"
    assert entries["AI4X_Jumper2021"].ids.doi == "10.1038/s41586-021-03819-2"
    for key in ("AI4X_Schutt2017", "AI4X_Xie2018"):
        assert "related work" in parsed.contexts[key][0].text
        assert "[2" not in parsed.contexts[key][0].text
    result = CliRunner().invoke(app, ["audit", str(path), "--no-network", "--format", "json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["input_kind"] == "docx"
    assert report["summary"]["total_entries"] == report["summary"]["cited"] == 9
    assert report["uncited"] == report["cited_but_missing"] == []
    assert "WARNING: WORD AUDITS CHECK ONLY" in result.stderr
