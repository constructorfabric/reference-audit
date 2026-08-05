"""Sentence-span extraction for citing contexts — shared by the `.tex` and the PDF/TEI front ends.

`parsing.tex.parse_citation_contexts` (LaTeX source) and `pdf.tei.parse_tei` (GROBID output) must
agree on what "the citing sentence" *is*, or a `CitationContext` would mean something different
depending on which format the manuscript arrived in — and the alignment check compares them against
the same abstracts. These three helpers are that single definition. Each front end adds only its own
noise stripping on top: `_clean_context` for LaTeX macros, nothing for TEI (whose text is already
plain prose).

This module is pure: no I/O, no network, no LaTeX and no XML knowledge.
"""

from __future__ import annotations

import re

_SENT_END_RE = re.compile(r"[.!?]")

# A cleaned context shorter than this (bare macro shells, a lone trailing cite) is extended with its
# preceding sentence so there is an actual claim to judge.
MIN_CONTEXT_CHARS = 15


def collapse(span: str) -> str:
    """Collapse whitespace runs and strip — the final step of every context extraction."""
    return re.sub(r"\s+", " ", span).strip()


def sentence_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Bounds of the sentence (or paragraph-bounded fragment) containing text[start:end]. A sentence
    boundary is a `.`/`!`/`?` followed by whitespace; a blank line also bounds it.

    A zero-width span (`start == end`) is valid and returns the sentence surrounding that offset —
    the TEI front end deletes each citation marker's surface text (`[13]`, `(Chan, 2019)`), which is
    not part of the citing claim, and asks for the sentence around the position where it stood.
    """
    left = 0
    for m in _SENT_END_RE.finditer(text, 0, start):
        e = m.end()
        if e < len(text) and text[e].isspace():
            left = e
    para = text.rfind("\n\n", left, start)
    if para != -1:
        left = para + 2

    right = len(text)
    for m in _SENT_END_RE.finditer(text, end):
        e = m.end()
        if e >= len(text) or text[e].isspace():
            right = e
            break
    para = text.find("\n\n", end, right)
    if para != -1:
        right = para
    return left, right
