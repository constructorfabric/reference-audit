"""LaTeX → Unicode for `.bib` values: markup is decoded, BibTeX literals are not."""

import pytest

from reference_audit.parsing.latex import decode_latex


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ("{\\\"o}ber", "öber"),
        ("\\\"{o}ber", "öber"),
        ("Garc{\\'\\i}a", "García"),
        ("Stra{\\ss}e", "Straße"),
        ("Erd\\H{o}s", "Erdős"),
        ("{\\l}ukasz", "łukasz"),
        ("\\textit{Deep} \\emph{Nets}", "Deep Nets"),
        ("{GAN}s", "GANs"),
        ("AT\\&T", "AT&T"),
        ("5\\% of", "5% of"),
        ("pp.\\ 1--88", "pp. 1--88"),
        ("\\url{https://a.org/~u/x_y}", "https://a.org/~u/x_y"),
        ("\\href{https://a.org}{Label}", "Label <https://a.org>"),
        ("$\\alpha$-helix", "$α$-helix"),
    ],
)
def test_markup_is_decoded(raw, text):
    assert decode_latex(raw) == text


@pytest.mark.parametrize(
    "literal",
    [
        "https://a.org/~u/x_y?q=1%20b#f",  # `%` is no comment, `~` no space, `#` no parameter
        "5% to 26%",
        "A & B &amp; C d&apos;Amore",      # `&` is no column separator
        "65--92 a---b ``q''",               # no typographic dashes or quotes
        "10.1002/(SICI)1097-4636(199707)",
    ],
)
def test_bibtex_literals_are_left_as_written(literal):
    assert decode_latex(literal) == literal
