"""LaTeX citation extraction.

`strip_comments`/`strip_verbatim` are lifted from `sciwrite-lint/tex_parser.py`. We extend
cite extraction to include `\\nocite` (chan2019lenia is nocite-only in the pilot) and to
resolve `\\input`/`\\include` so multi-file manuscripts aren't miscounted.
"""

from __future__ import annotations

import re
from pathlib import Path

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
