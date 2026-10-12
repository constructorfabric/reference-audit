r"""LaTeX markup in `.bib` field values → plain Unicode text.

bibtexparser 2 decodes no LaTeX itself (its `LatexDecodingMiddleware` is a thin pylatexenc wrapper).
Its default decoder reads a field value as a LaTeX *document*, which a BibTeX value is not:

* a bare `%` starts a comment, so `https://x.org/a%20b` loses everything after `a`;
* `&` is a table column separator, so `d&apos;Amore` becomes `d   apos;Amore` and the HTML reference
  can no longer be decoded (`parsing.entities`);
* `~` is a non-breaking space, so `https://x.org/~user` is corrupted;
* `--`, `---`, ``` `` ``` and `''` become typographic dashes and quotes, so `pages = {65--92}`
  reads `65–92`.

Here those characters are literal: `%` is escaped before parsing and the context has no "specials".
What is decoded is the markup that changes how a reference reads in a database: accents
(`{\'e}` → é, `\"{o}` → ö, `\ss` → ß), text-mode macros (`\textit{Deep}` → Deep, `\&` → &,
`\url{x}` → x, `\href{x}{X}` → `X <x>`) and symbols inside math (`$\alpha$` → `$α$`).
"""

from __future__ import annotations

import re

from pylatexenc import latexwalker
from pylatexenc.latex2text import LatexNodes2Text, MacroTextSpec, get_default_latex_context_db
from pylatexenc.macrospec import LatexContextDb, MacroSpec

# A `%` that is not already escaped as `\%`.
_BARE_PERCENT_RE = re.compile(r"(?<!\\)%")


class LatexDecodeError(ValueError):
    """pylatexenc failed on a value; the field cannot be read as text."""


def _text_context() -> LatexContextDb:
    """pylatexenc's default macro → text rules, without its specials (`&`, `~`, `--`, quotes)."""
    base = get_default_latex_context_db()
    db = LatexContextDb()
    # `\url{x}` is the URL itself, not pylatexenc's `<x>`.
    db.add_context_category("bibtex", macros=[MacroTextSpec("url", simplify_repl="%s")])
    for cat in base.categories():
        db.add_context_category(
            cat,
            macros=list(base.iter_macro_specs([cat])),
            environments=list(base.iter_environment_specs([cat])),
        )
    db.set_unknown_macro_spec(base.unknown_macro_spec)
    db.set_unknown_environment_spec(base.unknown_environment_spec)
    return db


def _parse_context() -> LatexContextDb:
    """The walker's default argument specs, plus `\\href{url}{text}`, which it does not know.

    Without it `\\href` is parsed with no arguments and its text rule raises an `IndexError`.
    """
    db = latexwalker.get_default_latex_context_db()
    db.add_context_category("bibtex", prepend=True, macros=[MacroSpec("href", "{{")])
    return db


_TEXT = LatexNodes2Text(
    latex_context=_text_context(), math_mode="with-delimiters", keep_braced_groups=False
)
_PARSE_CONTEXT = _parse_context()


def decode_latex(value: str) -> str:
    """`value` with its LaTeX markup decoded to Unicode; `%`, `&`, `~` and dashes stay literal.

    Raises `LatexDecodeError` when pylatexenc fails on the value.
    """
    if "\\" not in value and "{" not in value and "$" not in value:
        return value
    escaped = _BARE_PERCENT_RE.sub(r"\\%", value)
    try:
        walker = latexwalker.LatexWalker(
            escaped, latex_context=_PARSE_CONTEXT, tolerant_parsing=True
        )
        nodes, _, _ = walker.get_latex_nodes()
        return _TEXT.nodelist_to_text(nodes)
    except Exception as e:  # noqa: BLE001 — any pylatexenc failure means the value is unreadable
        raise LatexDecodeError(f"{type(e).__name__}: {e}") from e
