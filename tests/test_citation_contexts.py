"""Step 2 (Citation Alignment): offline extraction of per-key citing contexts from .tex.

Pure/deterministic, no network — same slice as parse_cited_keys.
"""

from __future__ import annotations

from pathlib import Path

from reference_audit.parsing.tex import parse_citation_contexts


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "main.tex"
    p.write_text(body, encoding="utf-8")
    return p


def test_single_cite_context_strips_macro(tmp_path):
    tex = _write(tmp_path, r"Lenia exhibits open-ended evolution \citep{chan2019lenia}. Next.")
    ctx = parse_citation_contexts(tex)
    assert set(ctx) == {"chan2019lenia"}
    (c,) = ctx["chan2019lenia"]
    assert "Lenia exhibits open-ended evolution" in c.text
    assert "chan2019lenia" not in c.text        # the \cite macro is not part of the claim
    assert c.ordinal == 0 and c.command == "citep"


def test_multi_key_share_sentence(tmp_path):
    tex = _write(tmp_path, r"Models a and b show pattern P \citep{aaa,bbb}. Done.")
    ctx = parse_citation_contexts(tex)
    assert set(ctx) == {"aaa", "bbb"}
    assert "Models a and b show pattern P" in ctx["aaa"][0].text
    assert ctx["aaa"][0].text == ctx["bbb"][0].text   # v1 is sentence-granular
    assert ctx["aaa"][0].ordinal == 0 and ctx["bbb"][0].ordinal == 0


def test_repeated_key_gets_increasing_ordinals(tmp_path):
    tex = _write(tmp_path, r"First claim about it \cite{ref}. Second claim about it \cite{ref}.")
    ctx = parse_citation_contexts(tex)
    ords = [c.ordinal for c in ctx["ref"]]
    assert ords == [0, 1]
    assert "First claim about it" in ctx["ref"][0].text
    assert "Second claim about it" in ctx["ref"][1].text


def test_nocite_produces_no_context(tmp_path):
    tex = _write(tmp_path, r"Body text \cite{shown}. \nocite{hidden}")
    ctx = parse_citation_contexts(tex)
    assert "shown" in ctx
    assert "hidden" not in ctx


def test_comments_and_verbatim_excluded(tmp_path):
    tex = _write(
        tmp_path,
        "Real claim here \\citep{good}.\n"
        "% a commented \\citep{bad} line\n"
        "\\begin{verbatim}\n\\cite{code}\n\\end{verbatim}\n",
    )
    ctx = parse_citation_contexts(tex)
    assert set(ctx) == {"good"}


def test_bare_trailing_cite_folds_in_preceding_sentence(tmp_path):
    tex = _write(
        tmp_path,
        "Complexity grows unboundedly in these systems. \\citep{ref}. After.",
    )
    ctx = parse_citation_contexts(tex)
    # the cite's own "sentence" is empty, so the preceding claim is folded in
    assert "Complexity grows unboundedly in these systems" in ctx["ref"][0].text


def test_no_citations_returns_empty(tmp_path):
    tex = _write(tmp_path, "A paper with no citations at all.")
    assert parse_citation_contexts(tex) == {}
