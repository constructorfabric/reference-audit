"""LaTeX citation extraction.

`strip_comments`/`strip_verbatim` are lifted from `sciwrite-lint/tex_parser.py`. We extend
cite extraction to include `\\nocite` (chan2019lenia is nocite-only in the pilot) and to
resolve `\\input`/`\\include` so multi-file manuscripts aren't miscounted.
"""

from __future__ import annotations

import re
from pathlib import Path

from reference_audit.models import CitationContext

_VERBATIM_ENVS = ("lstlisting", "verbatim", "minted", "Verbatim", "alltt")
_VERBATIM_RE = re.compile(
    r"\\begin\{(" + "|".join(re.escape(e) for e in _VERBATIM_ENVS) + r")\}.*?\\end\{\1\}",
    re.DOTALL,
)
_VERB_INLINE_RE = re.compile(r"\\verb(.)(.*?)\1")

# \cite, \citep, \citet, \citeyearpar, \citeauthor, \citealt, \citealp, \nocite, ...
_CITE_RE = re.compile(r"\\(?:no)?cite[a-zA-Z]*\s*(?:\[[^\]]*\])*\s*\{([^}]*)\}")
_NOCITE_RE = re.compile(r"\\nocite\s*\{([^}]*)\}")
_INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")

# Same as _CITE_RE but capturing the command name and (optional) pre/post bracket args, so context
# extraction can name the command and skip \nocite (which carries no citing prose).
_CITE_CMD_RE = re.compile(
    r"\\(?P<cmd>(?:no)?cite[a-zA-Z]*)\s*(?:\[[^\]]*\])*\s*\{(?P<keys>[^}]*)\}"
)
_SENT_END_RE = re.compile(r"[.!?]")
_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+\*?")
# A cleaned context shorter than this (bare macro shells, a lone cite) is extended with its
# preceding sentence so the LLM has an actual claim to judge.
_MIN_CONTEXT_CHARS = 15


def strip_comments(text: str) -> str:
    r"""Remove LaTeX comments (% to end of line), preserving escaped \%."""
    out_lines: list[str] = []
    for line in text.split("\n"):
        buf: list[str] = []
        i = 0
        while i < len(line):
            if line[i] == "%" and (i == 0 or line[i - 1] != "\\"):
                break
            buf.append(line[i])
            i += 1
        out_lines.append("".join(buf))
    return "\n".join(out_lines)


def strip_verbatim(text: str) -> str:
    r"""Blank verbatim-like environments and inline \verb|...| (so cites inside code don't count)."""
    text = _VERBATIM_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    text = _VERB_INLINE_RE.sub("", text)
    return text


def _resolve_input(name: str, base_dir: Path) -> Path | None:
    candidate = (base_dir / name).expanduser()
    for p in (candidate, candidate.with_suffix(".tex")):
        if p.suffix == "":
            p = p.with_suffix(".tex")
        if p.is_file():
            return p
    return None


def _read_with_includes(path: Path, seen: set[Path], missing: list[str]) -> str:
    """Concatenate a tex file with its \\input/\\include targets (depth-first, cycle-safe).

    Unresolved include targets are appended to `missing` so the caller can warn that
    citation coverage is incomplete.
    """
    path = path.resolve()
    if path in seen or not path.is_file():
        return ""
    seen.add(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    text = strip_comments(text)  # strip before resolving includes (commented \input ignored)

    def _expand(m: re.Match[str]) -> str:
        name = m.group(1).strip()
        target = _resolve_input(name, path.parent)
        if target is None:
            missing.append(name)
            return ""
        return _read_with_includes(target, seen, missing)

    return _INPUT_RE.sub(_expand, text)


def parse_cited_keys(tex_path: str | Path) -> tuple[set[str], bool, list[str]]:
    """Return (cited_keys, nocite_star, missing_includes).

    `cited_keys` is the union of all `\\cite*` and `\\nocite` keys across the main file and
    its resolvable includes. `nocite_star` is True if `\\nocite{*}` appears (cite-everything).
    `missing_includes` lists `\\input`/`\\include` targets that could not be found on disk.
    """
    path = Path(tex_path)
    missing: list[str] = []
    text = _read_with_includes(path, set(), missing)
    text = strip_verbatim(text)

    nocite_star = bool(re.search(r"\\nocite\s*\{\s*\*\s*\}", text))

    keys: set[str] = set()
    for m in _CITE_RE.finditer(text):
        for raw in m.group(1).split(","):
            key = raw.strip()
            if key and key != "*":
                keys.add(key)
    return keys, nocite_star, sorted(set(missing))


def _clean_context(span: str) -> str:
    """Strip LaTeX noise from a raw sentence span so what remains is the readable citing claim:
    drop the cite macros entirely, strip other `\\command` control words (keeping their braced
    argument *text*), and collapse braces/ties/whitespace."""
    span = _CITE_CMD_RE.sub(" ", span)     # the citation macro is not part of the claim
    span = _LATEX_CMD_RE.sub(" ", span)    # \emph, \textbf, ... — keep the argument text, drop the name
    span = span.replace("{", " ").replace("}", " ").replace("~", " ").replace("\\", " ")
    return re.sub(r"\s+", " ", span).strip()


def _sentence_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Bounds of the sentence (or paragraph-bounded fragment) containing text[start:end]. A sentence
    boundary is a `.`/`!`/`?` followed by whitespace; a blank line also bounds it."""
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


def parse_citation_contexts(tex_path: str | Path) -> dict[str, list[CitationContext]]:
    """Per-key citing contexts: the sentence(s) around each `\\cite`-family occurrence — the *reason*
    each work is cited — extracted offline from the `.tex` and its resolvable includes.

    `\\nocite` is skipped (it carries no citing prose). A key cited several times yields several
    contexts, each with a 0-based `ordinal` in document order. When a citation's own sentence is too
    short to carry a claim (a bare trailing cite), the preceding sentence is folded in. Keys sharing
    one sentence each get that whole sentence (v1 is sentence-granular, not clause-granular).
    """
    path = Path(tex_path)
    text = _read_with_includes(path, set(), [])
    text = strip_verbatim(text)

    contexts: dict[str, list[CitationContext]] = {}
    counts: dict[str, int] = {}
    for m in _CITE_CMD_RE.finditer(text):
        cmd = m.group("cmd")
        if cmd.startswith("nocite"):
            continue
        left, right = _sentence_span(text, m.start(), m.end())
        cleaned = _clean_context(text[left:right])
        if len(cleaned) < _MIN_CONTEXT_CHARS:
            # fold in the preceding sentence so there is an actual claim to judge
            prev_left, _ = _sentence_span(text, max(left - 2, 0), max(left - 1, 0))
            cleaned = _clean_context(text[prev_left:right])
        for raw in m.group("keys").split(","):
            key = raw.strip()
            if not key or key == "*":
                continue
            ordinal = counts.get(key, 0)
            counts[key] = ordinal + 1
            contexts.setdefault(key, []).append(
                CitationContext(key=key, text=cleaned, ordinal=ordinal, command=cmd)
            )
    return contexts
