"""The words of a cited title that a word index can match.

Shared by every title search that requires *all* words to match: DBLP's SPARQL word index and the
local ClickHouse full-text indexes. Under all-words matching a single word the index spells
differently hides the real record, so only words that are certain to be indexed the same way are
kept.
"""

from __future__ import annotations

import html
import re

# LaTeX that survives .bib decoding and is no word of the indexed title: inline math (`$\epsilon$`,
# which the databases store as "ε") and bare commands.
_LATEX_MATH_RE = re.compile(r"\$[^$]*\$")
_LATEX_CMD_RE = re.compile(r"\\[A-Za-z]+")
_WORD_RE = re.compile(r"[^\W_]+")


def title_search_words(title: str) -> list[str]:
    """The searchable words of `title`, lowercased, in order.

    HTML entities are decoded first (`Don&apos;t` would otherwise yield a phantom word "apos"), then
    LaTeX math and commands are dropped. Only plain ASCII alphanumeric words are kept: a word with a
    non-ASCII letter ("Kübler") or a LaTeX remnant could be indexed differently from how the `.bib`
    spells it.
    """
    text = _LATEX_CMD_RE.sub(" ", _LATEX_MATH_RE.sub(" ", html.unescape(title)))
    words = (w.lower() for w in _WORD_RE.findall(text))
    return [w for w in words if w.isascii() and w.isalnum()]
