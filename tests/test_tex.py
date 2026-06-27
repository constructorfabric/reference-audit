"""LaTeX citation extraction: \\cite variants, \\nocite, missing includes."""

from reference_audit.parsing.tex import parse_cited_keys


def test_cite_variants_and_nocite(tmp_path):
    tex = tmp_path / "doc.tex"
    tex.write_text(
        r"""
        \documentclass{article}\begin{document}
        Body \cite{a,b} and \citep{c} and \citet[see][p.2]{d}.
        \nocite{e}
        % \cite{commented_out}
        \begin{verbatim}\cite{in_verbatim}\end{verbatim}
        \end{document}
        """,
        encoding="utf-8",
    )
    keys, nocite_star, missing = parse_cited_keys(tex)
    assert keys == {"a", "b", "c", "d", "e"}
    assert nocite_star is False
    assert missing == []


def test_missing_include_is_reported(tmp_path):
    tex = tmp_path / "main.tex"
    tex.write_text(r"\cite{here}\input{nonexistent_section}", encoding="utf-8")
    keys, _, missing = parse_cited_keys(tex)
    assert keys == {"here"}
    assert missing == ["nonexistent_section"]


def test_resolves_existing_include(tmp_path):
    (tmp_path / "sec.tex").write_text(r"\cite{from_include}", encoding="utf-8")
    main = tmp_path / "main.tex"
    main.write_text(r"\cite{from_main}\input{sec}", encoding="utf-8")
    keys, _, missing = parse_cited_keys(main)
    assert keys == {"from_main", "from_include"}
    assert missing == []


def test_pilot_citations(pilot_tex):
    keys, nocite_star, missing = parse_cited_keys(pilot_tex)
    # chan2019lenia is cited directly AND via \nocite
    assert "chan2019lenia" in keys
    assert "vanchurin2022toward" in keys
    assert nocite_star is False
    # the manuscript \input's six files that are not provided in tests/
    assert len(missing) == 6
    assert "section3.tex" in missing
