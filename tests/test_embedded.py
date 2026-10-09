"""PDF input from the BibTeX records a PDF embeds for its cited works (`pdf.embedded`).

The fixture `tests/fixtures/embedded/paper.pdf` was compiled from `paper.tex` + `paper.bib` with the
AI4X template's `ai4x.cls` and `ai4x.bst`, so `paper.bib` is the oracle for what the PDF must carry:
its cited entries, with only the printed fields and identifiers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter

from reference_audit.config import AuditConfig
from reference_audit.parsing.bib import parse_bib, parse_bib_text
from reference_audit.pdf.embedded import (
    ATTACHMENT_NAME,
    decode_hex_payload,
    read_embedded_references,
    reference_list_text,
    title_printed,
)
from reference_audit.pipeline import (
    AuditPipeline,
    EmptyBibliographyError,
    build_embedded_parse_report,
)
from reference_audit.report import render_text

from test_pdf_pipeline import _ONE_ARTICLE, RecordingAdapter, StubGrobid, _tei

FIXTURE = Path(__file__).parent / "fixtures" / "embedded"
PAPER_PDF = FIXTURE / "paper.pdf"


def _pdf(tmp_path: Path, attachments: dict[str, bytes] | None = None) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    for name, data in (attachments or {}).items():
        writer.add_attachment(name, data)
    path = tmp_path / "paper.pdf"
    with path.open("wb") as f:
        writer.write(f)
    return path


# ---------------------------------------------------------------- decoding


def test_hex_payload_ignores_the_whitespace_tex_and_bibtex_wrap_lines_with():
    assert decode_hex_payload(b"4061 72\n  74\r\n6963 6C65") == "@article"


def test_hex_payload_is_utf8():
    assert decode_hex_payload("Schrödinger".encode().hex().encode()) == "Schrödinger"


@pytest.mark.parametrize(
    "payload, problem",
    [(b"", "is empty"), (b" \n", "is empty"), (b"40zz", "not hex"), (b"406", "odd number")],
)
def test_a_malformed_hex_payload_is_rejected_with_the_reason(payload, problem):
    with pytest.raises(ValueError, match=problem):
        decode_hex_payload(payload)


# ---------------------------------------------------------------- reading the attachment


def test_the_template_pdf_carries_exactly_its_cited_entries():
    embedded = read_embedded_references(PAPER_PDF)
    assert embedded.problem is None and embedded.bib_text is not None
    entries, _, _ = parse_bib(FIXTURE / "paper.bib")
    oracle = {e.key: e for e in entries}
    got, got_twins, got_unparsed = parse_bib_text(embedded.bib_text)
    assert [e.key for e in got] == ["jumper2021alphafold", "schutt2017schnet", "special"], (
        "the cited entries, in printed order; the uncited one is not exported"
    )
    assert got_twins == [] and got_unparsed == []
    for e in got:
        assert e.content_hash == oracle[e.key].content_hash, "same entry as from the .bib"
    assert "abstract" not in got[0].raw_fields, "a field that is not printed is not exported"
    special = got[2]
    assert special.ids.url == "https://example.org/a%20b?x=1#frag", "%, # survive the trip"
    assert "$" in special.title and "50%" in special.title


def test_a_pdf_without_the_attachment_is_the_ordinary_case(tmp_path):
    embedded = read_embedded_references(_pdf(tmp_path, {"notes.txt": b"hello"}))
    assert embedded.bib_text is None and embedded.problem is None


def test_a_malformed_attachment_is_a_reported_problem(tmp_path):
    embedded = read_embedded_references(_pdf(tmp_path, {ATTACHMENT_NAME: b"not hex"}))
    assert embedded.bib_text is None
    assert ATTACHMENT_NAME in embedded.problem and "not hex-encoded" in embedded.problem


def test_an_unreadable_pdf_is_a_reported_problem(tmp_path):
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    embedded = read_embedded_references(path)
    assert embedded.bib_text is None
    assert "could not look for embedded BibTeX records" in embedded.problem


# ---------------------------------------------------------------- printed titles


def test_every_title_of_the_fixture_is_found_in_its_printed_reference_list():
    printed = reference_list_text(PAPER_PDF)
    entries, _, _ = parse_bib(FIXTURE / "paper.bib")
    cited = [e for e in entries if e.key != "uncited"]
    assert all(title_printed(e.title, printed) for e in cited)
    (uncited,) = [e for e in entries if e.key == "uncited"]
    assert title_printed(uncited.title, printed) is False


def test_title_lookup_survives_hyphenation_ligatures_accents_and_case():
    printed = "[1] A. B. Graph neu-\nral net-\nworks for ﬁnding Schrödinger cats. Nature, 2020."
    assert title_printed("Graph Neural Networks for Finding Schrödinger Cats", printed)


def test_title_lookup_skips_math_and_commands_whose_typeset_form_differs():
    printed = "[1] Of the α-helix: a study in vivo."
    assert title_printed(r"Of the $\alpha$-helix: a study \emph{in vivo}", printed)


def test_a_title_absent_from_the_printed_list_is_reported_false():
    assert title_printed("A mobile robotic chemist", "[1] An imaginary robot.") is False


def test_a_title_too_short_to_look_up_is_unknown_not_false():
    assert title_printed(r"$\alpha$ or $\beta$", "[1] Something else.") is None


# ---------------------------------------------------------------- the parse report


def test_embedded_report_declares_its_provenance():
    report = build_embedded_parse_report(PAPER_PDF, read_embedded_references(PAPER_PDF).bib_text)
    assert report.input_kind == "pdf_embedded"
    assert report.citation_linking == "available"
    assert all(a.entry.cited for a in report.entries)
    assert report.uncited == [] and report.cited_but_missing == []
    assert report.summary["titles_not_in_printed_references"] == 0
    assert any(ATTACHMENT_NAME in n for n in report.notes)
    text = render_text(report)
    assert "commented twins" not in text, "inapplicable to embedded records"


def test_a_record_whose_title_is_not_printed_is_flagged():
    bib = read_embedded_references(PAPER_PDF).bib_text.replace(
        "Highly accurate protein structure prediction", "Moderately accurate guesses"
    )
    report = build_embedded_parse_report(PAPER_PDF, bib)
    flagged = [a.entry.key for a in report.entries if any("not found in" in i for i in a.issues)]
    assert flagged == ["jumper2021alphafold"]
    assert report.summary["titles_not_in_printed_references"] == 1
    assert any("--ignore-embedded" in n for n in report.notes)


def test_records_without_a_readable_entry_are_an_empty_bibliography():
    with pytest.raises(EmptyBibliographyError, match="no readable BibTeX entry"):
        build_embedded_parse_report(PAPER_PDF, "% nothing here\n")


# ---------------------------------------------------------------- run_pdf: embedded first, GROBID fallback


async def _run(path: Path, config: AuditConfig, grobid: StubGrobid):
    pipeline = AuditPipeline(config, adapters=[RecordingAdapter()], grobid=grobid)
    try:
        return await pipeline.run_pdf(path)
    finally:
        await pipeline.aclose()


async def test_embedded_records_are_audited_without_grobid():
    grobid = StubGrobid(_tei(_ONE_ARTICLE))
    report = await _run(PAPER_PDF, AuditConfig(use_llm=False), grobid)
    assert grobid.calls == []
    assert report.input_kind == "pdf_embedded"
    assert [a.entry.key for a in report.entries] == [
        "jumper2021alphafold", "schutt2017schnet", "special",
    ]
    assert "verdicts" in report.summary


async def test_ignore_embedded_extracts_with_grobid():
    grobid = StubGrobid(_tei(_ONE_ARTICLE))
    report = await _run(PAPER_PDF, AuditConfig(use_llm=False, use_embedded_references=False), grobid)
    assert grobid.calls == [PAPER_PDF]
    assert report.input_kind == "pdf"


async def test_a_pdf_without_records_goes_to_grobid_silently(tmp_path):
    grobid = StubGrobid(_tei(_ONE_ARTICLE))
    report = await _run(_pdf(tmp_path), AuditConfig(use_llm=False), grobid)
    assert len(grobid.calls) == 1 and report.input_kind == "pdf"
    assert not any("embedded" in n for n in report.notes)


async def test_malformed_records_go_to_grobid_with_the_reason_noted(tmp_path):
    grobid = StubGrobid(_tei(_ONE_ARTICLE))
    path = _pdf(tmp_path, {ATTACHMENT_NAME: b"4061"})  # "@a": hex, but no entry
    report = await _run(path, AuditConfig(use_llm=False), grobid)
    assert len(grobid.calls) == 1 and report.input_kind == "pdf"
    assert any("no readable BibTeX entry" in n and "GROBID instead" in n for n in report.notes)


async def test_alignment_on_embedded_records_is_declared_unchecked_once():
    report = await _run(
        PAPER_PDF, AuditConfig(use_llm=False, check_alignment=True), StubGrobid(_tei(""))
    )
    assert sum("citation alignment was not checked" in n for n in report.notes) == 1
    assert not any("alignment" in i for a in report.entries for i in a.issues)
