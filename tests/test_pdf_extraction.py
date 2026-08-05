"""Extraction fidelity against a compiled-from-source oracle. Needs LaTeX + GROBID; gated.

Each test document is compiled from its own ``.tex`` + ``.bib``, so that ``.bib`` is ground truth for
whatever GROBID then reads back out of the PDF. See ``tests/pdf_oracle.py`` for how extracted
references are matched to printed ones without any shared keys.

**The pinned numbers are a regression floor, not a fidelity guarantee.** They were observed on the
configuration recorded in ``EXPECTED`` below and then relaxed by a small margin, so a drop in
extraction quality fails this test. They say nothing about GROBID's accuracy on arbitrary
third-party PDFs: these bibliographies were typeset by BibTeX from clean data, in two styles, with no
scanning artefacts. Real-world PDFs are messier — see the realism caveat in the feature docs.

Both remaining known defects are documented at their thresholds rather than silently absorbed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import median

import pytest

from reference_audit.matching.features import title_ratio
from reference_audit.parsing.tex import parse_citation_contexts
from reference_audit.pdf.tei import parse_tei
from pdf_fixtures import BUILD_DIRNAME, compile_pdf, fetch_tei, preflight
from pdf_oracle import compare, ground_truth

pytestmark = [pytest.mark.pdf_build, pytest.mark.grobid]


@dataclass(frozen=True)
class Expected:
    """Regression floors for one document version.

    Observed 2026-08-02 with GROBID docker.io/grobid/grobid:0.8.2.1-crf and TeX Live 2026
    (pdfTeX 1.40.29), figures replaced by generated placeholders. Recorded values are in the comment
    on each field; the pinned bound leaves ~2% of N of headroom so a CRF that is not bit-stable does
    not turn a harmless change into a red build.
    """

    slug: str
    version: str
    printed: int              # references BibTeX actually emitted
    matched_min: int
    extracted_delta_max: int  # |extracted - printed|
    spurious_max: int
    doi_wrong_max: int        # extracted a DOI that disagrees with the printed one
    doi_unexpected_max: int   # extracted a DOI for an entry that has none
    year_wrong_max: int
    year_missing_max: int
    linked_keys_min: int
    context_agreement_min: float
    positional_min: int | None   # None where multi-column reading order makes this uninformative

    @property
    def id(self) -> str:
        return f"{self.slug}/{self.version}"


EXPECTED = [
    # Single-column pandoc article, numeric `abbrv` style. Extraction is exact here, so the floors sit
    # right under the observed values: 28/28 matched, 28/28 in printed order, no spurious references,
    # no wrong or missing years, and every reference's citations linked.
    Expected(
        slug="directing-open-ended-evolution",
        version="initial",
        printed=28,
        matched_min=27,              # observed 28
        extracted_delta_max=1,       # observed 0
        spurious_max=1,              # observed 0
        doi_wrong_max=0,             # observed 0 — `abbrv` prints no DOIs, so none can be misread
        doi_unexpected_max=0,        # observed 0
        year_wrong_max=0,            # observed 0
        year_missing_max=2,          # observed 0
        linked_keys_min=26,          # observed 28
        context_agreement_min=0.90,  # observed 1.00
        positional_min=26,           # observed 28
    ),
    # Two-column ICML class, author-year `icml2026` style. Harder on every axis, and it carries the one
    # unrecoverable defect: `ghate-etal-2025-biases` prints DOI 10.18653/v1/2025.findings-acl.955, and
    # GROBID reads it as ...findings-acl — its own raw-reference capture is cut at the same point, so
    # the tail exists nowhere in the TEI and `_resolve_doi`'s extend-only repair has nothing to work
    # from. Pinned at 1 with this explanation rather than asserting a 0 we do not achieve.
    Expected(
        slug="position-align-ai-to-aspirations",
        version="initial",
        printed=115,
        matched_min=105,             # observed 107
        extracted_delta_max=3,       # observed 1 (114 extracted)
        spurious_max=10,             # observed 7
        doi_wrong_max=1,             # observed 1 — the unrecoverable truncation described above
        # observed 1: GROBID gave `pew2023climate` (a report with no DOI) the DOI 10.1177/07067437221082854,
        # which appears nowhere in that reference's text. Its own cross-reference bleed; nothing in the
        # TEI lets the mapper tell this apart from a legitimately-read DOI, so it is measured, not fixed.
        doi_unexpected_max=1,
        year_wrong_max=6,            # observed 4 — GROBID reading a year out of a DOI string
        year_missing_max=48,         # observed 40 — benign: the field check reports it as unverified
        linked_keys_min=52,          # observed 56
        context_agreement_min=0.90,  # observed 55/56
        positional_min=None,         # observed 22/107; two-column reading order, not a quality signal
    ),
    Expected(
        slug="position-align-ai-to-aspirations",
        version="polished",
        printed=134,
        matched_min=122,             # observed 125
        extracted_delta_max=5,       # observed 3 (137 extracted — mild over-segmentation)
        spurious_max=16,             # observed 12
        doi_wrong_max=0,             # observed 0, at 78/78 DOI precision
        doi_unexpected_max=0,        # observed 0
        year_wrong_max=5,            # observed 3
        year_missing_max=42,         # observed 34
        linked_keys_min=64,          # observed 68
        context_agreement_min=0.90,  # observed 66/68
        positional_min=None,         # observed 70/125
    ),
]


@pytest.fixture(scope="module")
def _built() -> dict:
    return {}


def _oracle(exp: Expected, grobid_url: str, cache: dict):
    """Compile + extract once per document version, then reuse across this module's assertions."""
    if exp.id in cache:
        return cache[exp.id]

    from conftest import DocumentVersion

    doc = DocumentVersion(exp.slug, exp.version)
    plan = preflight(doc)
    build = doc.tex.parent / BUILD_DIRNAME / doc.version
    pdf = build / f"{doc.version}.pdf"
    if not pdf.is_file():
        pdf = compile_pdf(plan)
    tei_cache = build / f"{doc.version}.tei.xml"
    if not tei_cache.is_file():
        tei_cache.write_text(fetch_tei(pdf, grobid_url), encoding="utf-8")

    refs = parse_tei(tei_cache.read_text(encoding="utf-8"))
    truth = ground_truth(doc.bib, build / f"{doc.version}.bbl")
    cache[exp.id] = (doc, refs, compare(truth, refs.entries))
    return cache[exp.id]


@pytest.mark.parametrize("exp", EXPECTED, ids=[e.id for e in EXPECTED])
def test_reference_extraction_fidelity(
    exp: Expected, grobid_url, require_latex, require_grobid, _built, capsys
):
    _doc, _refs, result = _oracle(exp, grobid_url, _built)
    with capsys.disabled():
        # Always print the full table before asserting: when a floor is breached, the surrounding
        # numbers are what identify which axis regressed.
        print("\n" + result.table(exp.id))

    assert result.n_truth == exp.printed, "the compiled bibliography changed size — re-pin the floors"
    assert abs(result.n_extracted - result.n_truth) <= exp.extracted_delta_max
    assert result.matched >= exp.matched_min
    assert len(result.spurious) <= exp.spurious_max

    # A fabricated identifier is the gravest extraction error: it points the auditor at a real but
    # different document, so the report would confidently describe the wrong paper. The two ways it
    # can happen are bounded separately, because only one of them is visible as a disagreement.
    assert result.doi_wrong <= exp.doi_wrong_max
    assert result.doi_unexpected <= exp.doi_unexpected_max
    # Every extracted DOI is accounted for: correct, disagreeing, or attached to a DOI-less entry.
    assert result.doi_extracted == result.doi_correct + result.doi_fabricated

    assert result.year_wrong <= exp.year_wrong_max
    assert result.year_missing <= exp.year_missing_max

    if exp.positional_min is not None:
        assert result.positional_agreement >= exp.positional_min


@pytest.mark.parametrize("exp", EXPECTED, ids=[e.id for e in EXPECTED])
def test_citation_context_extraction(
    exp: Expected, grobid_url, require_latex, require_grobid, _built, capsys
):
    """Citing contexts recovered from the PDF must mean the same thing as the LaTeX-derived ones.

    This is what makes `--check-citations` usable on a PDF: the alignment check judges these sentences
    against the cited work's abstract, so a garbled or mis-attributed context would produce a wrong
    finding about the author's writing.
    """
    doc, refs, result = _oracle(exp, grobid_url, _built)
    tex_contexts = parse_citation_contexts(doc.tex)
    extracted_to_truth = {p.got.key: p.truth.key for p in result.pairs}

    # 1. Referential integrity is exact, not a tolerance: a marker pointing at a bibliography entry
    #    that does not exist would be a mapper bug.
    assert refs.dangling_targets == []
    assert set(refs.contexts) <= {e.key for e in refs.entries}
    assert all(c.text for group in refs.contexts.values() for c in group)
    assert all(
        c.ordinal == i for group in refs.contexts.values() for i, c in enumerate(group)
    ), "ordinals must be dense and in document order, so findings are reproducible"

    linked_truth_keys = {
        extracted_to_truth[k] for k in refs.contexts if k in extracted_to_truth
    }
    assert len(linked_truth_keys) >= exp.linked_keys_min

    common = linked_truth_keys & set(tex_contexts)
    truth_to_extracted = {v: k for k, v in extracted_to_truth.items()}
    within_one = sum(
        1
        for key in common
        if abs(len(refs.contexts[truth_to_extracted[key]]) - len(tex_contexts[key])) <= 1
    )
    # Exact per-key counts are unreachable: \citep{a,b} renders as one marker or several depending on
    # the style, so ±1 is the honest bound.
    agreement = within_one / len(common)

    similarities = [
        title_ratio(pdf_ctx.text, tex_ctx.text)
        for key in common
        for pdf_ctx, tex_ctx in zip(
            refs.contexts[truth_to_extracted[key]], tex_contexts[key], strict=False
        )
    ]
    with capsys.disabled():
        print(
            f"\n--- {exp.id} contexts ---\n"
            f"  keys linked            : {len(linked_truth_keys)} (of {len(tex_contexts)} citing keys "
            f"in the .tex)\n"
            f"  count agreement (+-1)  : {within_one}/{len(common)} ({agreement:.1%})\n"
            f"  median text similarity : {median(similarities):.2f} (n={len(similarities)})\n"
            f"  unlinked markers       : {refs.unresolved_markers} of {refs.markers_seen}"
        )

    assert agreement >= exp.context_agreement_min
    # The two front ends share one sentence-splitting definition (`parsing/context.py`), so a PDF
    # context and its .tex counterpart should be near-identical text. A median well below 1.0 would
    # mean that shared definition has drifted, or that the PDF text layer is being mangled.
    assert median(similarities) >= 0.85


def test_pilot_extraction_is_exact(grobid_url, require_latex, require_grobid, _built):
    """The single-column pilot is the development oracle: it should extract perfectly, not just well.

    Held separately from the tolerance-based floors above so that any loss of exactness here is
    visible immediately rather than absorbed by a margin sized for the harder two-column document.
    """
    exp = EXPECTED[0]
    _doc, refs, result = _oracle(exp, grobid_url, _built)
    assert result.n_extracted == 28
    assert result.matched == 28
    assert result.unmatched_truth == []
    assert result.spurious == []
    assert result.doi_wrong == 0
    assert result.year_wrong == 0
    assert result.year_missing == 0
    assert refs.unresolved_markers == 0, "every in-text marker should link in a single-column PDF"
