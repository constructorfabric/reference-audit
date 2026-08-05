r"""Compile a test document to PDF and fetch its GROBID TEI — the PDF-input extraction oracle.

The oracle: a document under ``tests/documents/<slug>/`` is compiled from its own ``.tex`` + ``.bib``,
so the ``.bib`` *is* ground truth for whatever GROBID then extracts from the resulting PDF. That makes
extraction fidelity measurable instead of assumed.

Not a pytest module (it is not named ``test_*``) and not a shipped CLI subcommand — it needs LaTeX and
GROBID, neither of which belongs in the audit tool's dependency surface. Run it by hand:

    uv run python tests/pdf_fixtures.py --all --preflight-only
    uv run python tests/pdf_fixtures.py --slug directing-open-ended-evolution --version initial \
        --compile --grobid http://localhost:8070 --write-tei
    uv run python tests/pdf_fixtures.py --all --check

Two rules the rest of this file exists to enforce:

* **Missing source files are never invented.** No stubs, no synthetic stand-in document. Every missing
  ``\input``/``.sty``/``.bst`` is enumerated in one pass and reported together, so the operator can
  supply them all in one trip rather than discovering them one LaTeX run at a time.
* **Figures are the one substitution**, because a bibliography does not depend on them. Missing images
  are replaced by generated placeholders, and that substitution is stated in the build report and
  recorded in the TEI header so no downstream reader mistakes the oracle PDF for the real article.

Nothing is ever written into the document's own directory — only into ``.build/<version>/``. The audit
tests glob ``tests/documents/<slug>/*.tex`` to discover versions and pin counts off those exact files,
so a stray staged file there would silently invent a document version.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).parent))

from conftest import DocumentVersion, discover_document_versions  # noqa: E402

from reference_audit.parsing.tex import _read_with_includes  # noqa: E402

# --- The GROBID request contract, shared with the client -----------------------------------------
# Imported rather than restated so a committed TEI fixture can never drift from what the audit
# pipeline actually asks for.
from reference_audit.pdf.grobid import (  # noqa: E402
    FULLTEXT_ENDPOINT,
    FULLTEXT_PARAMS,
    GROBID_IMAGE,
)

BUILD_DIRNAME = ".build"
FIXTURE_TEI_DIR = Path(__file__).parent / "fixtures" / "tei"

# Fixed timestamp (2016-01-01) so \today, /CreationDate and hyperref's pdfcreationdate do not change
# between runs. FORCE_SOURCE_DATE makes pdftex honor it for the PDF metadata too.
SOURCE_DATE_EPOCH = "1451606400"

# A committed TEI over this size is a decision for a human, not something to do silently.
MAX_TEI_BYTES = 1_000_000

# Which LaTeX engine each document's authors used (from the arXiv tarball's 00README.json; recorded in
# each slug's SOURCES.md). pdflatex is the default for anything unlisted.
COMPILERS: dict[str, str] = {
    "directing-open-ended-evolution": "xelatex",
    "position-align-ai-to-aspirations": "pdflatex",
}

_PACKAGE_RE = re.compile(r"\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_CLASS_RE = re.compile(r"\\documentclass\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_BIBSTYLE_RE = re.compile(r"\\bibliographystyle\s*\{([^}]+)\}")
_BIBLIOGRAPHY_RE = re.compile(r"\\bibliography\s*\{([^}]+)\}")
_BIBLATEX_RE = re.compile(r"\\addbibresource\s*\{([^}]+)\}|\\usepackage(?:\[[^\]]*\])?\{biblatex\}")
_GRAPHICS_RE = re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_GRAPHICSPATH_RE = re.compile(r"\\graphicspath\s*\{(.+?)\}\s*(?:\n|$)", re.DOTALL)
_FIGURE_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".eps")


# =================================================================================================
# Errors
# =================================================================================================


@dataclass(frozen=True)
class MissingInput:
    kind: Literal["input", "package", "class", "bst", "bib", "tool"]
    expected: str            # absolute path, or a bare tool name
    directive: str           # the verbatim LaTeX (or reason) that requires it
    tried: tuple[str, ...] = ()


class MissingDocumentInputsError(FileNotFoundError):
    """Every missing input for one document, enumerated in a single pass.

    No stubs are synthesized and there is no synthetic-document fallback: the operator supplies the
    real files. (Figures are the sole exception and are handled as placeholders, not as errors.)
    """

    def __init__(self, document_id: str, missing: list[MissingInput], notes: list[str]) -> None:
        self.document_id = document_id
        self.missing = tuple(missing)
        self.notes = tuple(notes)
        super().__init__(self._render())

    def _render(self) -> str:
        groups: list[tuple[str, str]] = [
            ("input", r"\input / \include targets"),
            ("class", "LaTeX document classes"),
            ("package", "LaTeX packages"),
            ("bst", "BibTeX styles"),
            ("bib", "bibliography files"),
            ("tool", "toolchain"),
        ]
        out = [
            f"cannot compile {self.document_id} to PDF.",
            f"{len(self.missing)} required input(s) are missing. No stubs are generated and there is "
            "no synthetic fallback --",
            "supply the real files at the paths below and re-run.",
            "",
        ]
        for kind, label in groups:
            items = [m for m in self.missing if m.kind == kind]
            if not items:
                continue
            out.append(f"  {label} ({len(items)}):")
            for m in items:
                out.append(f"    - {m.expected}")
                out.append(f"        required by: {m.directive}")
                if m.tried:
                    out.append(f"        tried: {', '.join(m.tried)}")
        for note in self.notes:
            out.append(f"  note: {note}")
        out.append(
            "  note: files reached only through a missing \\input cannot be scanned; supplying the"
        )
        out.append("        above may reveal further missing inputs on the next run.")
        return "\n".join(out)


class LatexCompileError(RuntimeError):
    """One failed compile stage, with the log excerpt that explains it."""

    def __init__(self, stage: str, returncode: int, log_path: Path, excerpt: str) -> None:
        self.stage = stage
        self.returncode = returncode
        self.log_path = log_path
        self.excerpt = excerpt
        super().__init__(
            f"{stage} failed (exit {returncode}).\n{excerpt}\n  full log: {log_path}"
        )


# =================================================================================================
# Placeholder figures
# =================================================================================================


def _png(width: int, height: int, tint: tuple[int, int, int]) -> bytes:
    """A minimal single-colour PNG, byte-identical on every run. No image library needed."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit truecolour
    row = b"\x00" + bytes(tint) * width                          # filter byte 0 + RGB pixels
    idat = zlib.compress(row * height, 9)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


PLACEHOLDER_PNG = _png(1600, 1200, (208, 212, 218))


def _write_placeholder(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PLACEHOLDER_PNG)


# =================================================================================================
# Preflight
# =================================================================================================


@dataclass
class BuildPlan:
    doc: DocumentVersion
    build_dir: Path
    compiler: str
    bib_stem: str                     # the name \bibliography{...} asks for
    bib_style: str
    placeholder_figures: list[str]     # figure paths that will be substituted
    notes: list[str]


def _kpsewhich(names: list[str]) -> set[str]:
    """Which of these files TeX can find. Matched by basename, since kpsewhich exits non-zero if
    ANY name is unresolved — so the exit code says nothing about the individual names."""
    if not names:
        return set()
    proc = subprocess.run(["kpsewhich", *names], capture_output=True, text=True, check=False)
    return {Path(line).name for line in proc.stdout.splitlines() if line.strip()}


def _guarded_lines(text: str) -> set[int]:
    r"""Line numbers holding an \IfFileExists guard — a \usepackage there is optional by design."""
    return {i for i, line in enumerate(text.splitlines()) if "IfFileExists" in line}


def _declared_packages(text: str) -> list[tuple[str, str, bool]]:
    r"""(name, directive, guarded) for every \usepackage / \RequirePackage / \documentclass."""
    guarded = _guarded_lines(text)
    out: list[tuple[str, str, bool]] = []
    for regex, _kind in ((_PACKAGE_RE, "package"), (_CLASS_RE, "class")):
        for m in regex.finditer(text):
            line_no = text.count("\n", 0, m.start())
            for name in m.group(1).split(","):
                name = name.strip()
                if name:
                    out.append((name, m.group(0), line_no in guarded))
    return out


def _graphics_targets(text: str, base: Path) -> list[tuple[str, str]]:
    """(path-as-written, directive) for each \\includegraphics that does not resolve on disk."""
    search_dirs = [base]
    gp = _GRAPHICSPATH_RE.search(text)
    if gp:
        search_dirs += [base / d for d in re.findall(r"\{([^}]*)\}", gp.group(1)) if d]
    missing: list[tuple[str, str]] = []
    for m in _GRAPHICS_RE.finditer(text):
        name = m.group(1).strip()
        found = any(
            (d / name).is_file() or any((d / (name + e)).is_file() for e in _FIGURE_EXTS)
            for d in search_dirs
        )
        if not found:
            missing.append((name, m.group(0)))
    return missing


def preflight(doc: DocumentVersion, *, compiler: str | None = None) -> BuildPlan:
    """Enumerate everything needed to compile `doc`, before any LaTeX process starts.

    Raises `MissingDocumentInputsError` listing every missing source file at once.
    """
    slug_dir = doc.tex.parent
    build_dir = slug_dir / BUILD_DIRNAME / doc.version
    engine = compiler or COMPILERS.get(doc.slug, "pdflatex")
    missing: list[MissingInput] = []
    notes: list[str] = []

    for path, why in ((doc.tex, "the manuscript"), (doc.bib, "the bibliography")):
        if not path.is_file():
            missing.append(MissingInput("input", str(path), f"{why} for {doc.id}"))
    if missing:
        raise MissingDocumentInputsError(doc.id, missing, notes)

    # \input/\include — reuse the parser's own resolution so a commented-out \input is ignored
    # exactly as the audit sees it.
    include_misses: list[str] = []
    text = _read_with_includes(doc.tex, set(), include_misses)
    for name in sorted(set(include_misses)):
        resolved = name if Path(name).suffix else f"{name}.tex"
        missing.append(
            MissingInput("input", str(slug_dir / resolved), f"\\input{{{name}}}")
        )

    # Packages and classes.
    declared = [(n, d, g) for n, d, g in _declared_packages(text) if not g]
    unresolved: list[tuple[str, str, str]] = []   # (name, filename, directive)
    for name, directive, _ in declared:
        kind = "class" if directive.startswith("\\documentclass") else "package"
        filename = f"{name}.{'cls' if kind == 'class' else 'sty'}"
        if not (slug_dir / filename).is_file():
            unresolved.append((kind, filename, directive))
    found = _kpsewhich([f for _, f, _ in unresolved])
    for kind, filename, directive in unresolved:
        if filename not in found:
            missing.append(
                MissingInput(
                    kind, filename, directive,   # type: ignore[arg-type]
                    tried=(str(slug_dir / filename), "kpsewhich (no hit)"),
                )
            )

    # Bibliography style and stem.
    if _BIBLATEX_RE.search(text):
        missing.append(
            MissingInput(
                "bib", "(biblatex is not supported by this harness)",
                "\\addbibresource / \\usepackage{biblatex}",
            )
        )
    style_m = _BIBSTYLE_RE.search(text)
    bib_style = style_m.group(1).strip() if style_m else "plain"
    bst = f"{bib_style}.bst"
    if not (slug_dir / bst).is_file() and bst not in _kpsewhich([bst]):
        missing.append(
            MissingInput(
                "bst", bst, f"\\bibliographystyle{{{bib_style}}}",
                tried=(str(slug_dir / bst), "kpsewhich (no hit)"),
            )
        )

    stems = [s.strip() for m in _BIBLIOGRAPHY_RE.finditer(text) for s in m.group(1).split(",")]
    stems = [s for s in stems if s]
    if len(set(stems)) != 1:
        missing.append(
            MissingInput(
                "bib",
                f"(expected exactly one \\bibliography{{...}} stem, found {sorted(set(stems))})",
                "\\bibliography{...}",
            )
        )
        bib_stem = doc.version
    else:
        bib_stem = stems[0]
        if bib_stem != doc.bib.stem:
            # NOT a missing file: the bibliography exists, only under the fixture's name. Staging it
            # under the referenced name beats asking for a committed duplicate of a file we have.
            notes.append(
                f"\\bibliography{{{bib_stem}}} needs no file from you -- {doc.bib.name} is staged "
                f"as {build_dir / (bib_stem + '.bib')}"
            )

    # Toolchain.
    for tool in (engine, "bibtex"):
        if not shutil.which(tool):
            missing.append(MissingInput("tool", tool, f"required to compile {doc.id}"))

    if missing:
        raise MissingDocumentInputsError(doc.id, missing, notes)

    # Figures: substituted, not demanded.
    figures = [name for name, _ in _graphics_targets(text, slug_dir)]
    if figures:
        notes.append(
            f"{len(figures)} figure(s) are absent and will be replaced by generated placeholders; "
            "page layout will differ from the published PDF, which does not affect reference "
            "extraction (the bibliography is its own section at the end of the document)"
        )

    return BuildPlan(
        doc=doc,
        build_dir=build_dir,
        compiler=engine,
        bib_stem=bib_stem,
        bib_style=bib_style,
        placeholder_figures=figures,
        notes=notes,
    )


# =================================================================================================
# Compile
# =================================================================================================


def _log_excerpt(log_path: Path) -> str:
    """The blocks of a LaTeX log that explain a failure, not the whole 2000-line transcript."""
    if not log_path.is_file():
        return "  (no log file was produced)"
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    starts = [
        i for i, line in enumerate(lines)
        if line.startswith("! ") or re.match(r"^.+?:\d+: ", line)
    ]
    if not starts:
        return "\n".join(f"  {line}" for line in lines[-40:])
    out: list[str] = []
    for start in starts[:3]:
        out.extend(f"  {line}" for line in lines[start : start + 16])
        out.append("")
    return "\n".join(out[:80])


def _blg_excerpt(blg_path: Path) -> str:
    if not blg_path.is_file():
        return "  (no .blg file was produced)"
    keep = ("I couldn't open", "I found no", "Warning--", "Repeated entry", "---line")
    lines = blg_path.read_text(encoding="utf-8", errors="replace").splitlines()
    hits = [line for line in lines if any(k in line for k in keep)]
    return "\n".join(f"  {line}" for line in (hits or lines[-20:])[:40])


def _env(plan: BuildPlan) -> dict[str, str]:
    slug_dir = plan.doc.tex.parent
    build = plan.build_dir
    env = dict(os.environ)
    env.update(
        # Build dir first so generated placeholder figures are found; the trailing empty entry keeps
        # the system TeX tree searchable.
        TEXINPUTS=f"{build}//:{slug_dir}//:",
        BIBINPUTS=f"{build}:{slug_dir}:",
        BSTINPUTS=f"{slug_dir}:",
        TEXMFVAR=str(build / ".texmfvar"),   # never touch the user's ~/.texlive
        SOURCE_DATE_EPOCH=SOURCE_DATE_EPOCH,
        FORCE_SOURCE_DATE="1",
    )
    return env


def compile_pdf(plan: BuildPlan, *, quiet: bool = True) -> Path:
    """Compile the document to `<build>/<version>.pdf`. Returns the PDF path."""
    doc, build = plan.doc, plan.build_dir
    build.mkdir(parents=True, exist_ok=True)

    shutil.copyfile(doc.bib, build / f"{plan.bib_stem}.bib")
    for name in plan.placeholder_figures:
        target = build / (name if Path(name).suffix else f"{name}.png")
        _write_placeholder(target)

    env = _env(plan)
    log_path = build / f"{doc.version}.log"
    # `\pdftrailerid{}` suppresses pdftex's random trailer /ID so repeated builds are byte-identical.
    # It is a pdftex primitive, so xelatex simply does not get it (and its PDFs are not byte-stable —
    # which is why --check compares semantically rather than by hash).
    prelude = "\\pdftrailerid{}" if plan.compiler == "pdflatex" else ""
    argv = [
        plan.compiler,
        "-interaction=nonstopmode",
        # Without -halt-on-error, an undefined control sequence scrolls past and still emits a
        # PARTIAL PDF — a silently truncated bibliography would then be measured as extraction loss.
        "-halt-on-error",
        "-file-line-error",
        f"-output-directory={build}",
        f"{prelude}\\input{{{doc.tex.name}}}",
    ]

    def run(stage: str, args: list[str], cwd: Path, log: Path, excerpt) -> None:
        proc = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise LatexCompileError(stage, proc.returncode, log, excerpt(log))
        if not quiet:
            print(f"    {stage}: ok")

    run("latex-1", argv, doc.tex.parent, log_path, _log_excerpt)
    run(
        "bibtex", ["bibtex", doc.version], build, build / f"{doc.version}.blg", _blg_excerpt
    )
    run("latex-2", argv, doc.tex.parent, log_path, _log_excerpt)
    run("latex-3", argv, doc.tex.parent, log_path, _log_excerpt)

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    if "Rerun to get" in log_text:
        run("latex-4", argv, doc.tex.parent, log_path, _log_excerpt)
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        if "Rerun to get" in log_text:
            raise LatexCompileError(
                "latex-4", 0, log_path,
                "  cross-references still unresolved after 4 passes — the document does not converge",
            )

    pdf = build / f"{doc.version}.pdf"
    if not pdf.is_file():
        raise LatexCompileError("latex-3", 0, log_path, "  no PDF was produced")
    return pdf


# =================================================================================================
# GROBID
# =================================================================================================


def fetch_tei(pdf: Path, grobid_url: str) -> str:
    """POST the PDF to GROBID using the audit pipeline's own client and parameters."""
    import asyncio

    from reference_audit.pdf.grobid import GrobidClient

    async def go() -> str:
        client = GrobidClient(grobid_url)
        try:
            return await client.fulltext_tei(pdf)
        finally:
            await client.aclose()

    return asyncio.run(go())


def _grobid_version(tei: str) -> str:
    m = re.search(r'<application[^>]*\bversion="([^"]+)"', tei)
    return m.group(1) if m else "unknown"


def provenance_text(plan: BuildPlan, tei: str, texlive: str) -> str:
    """Provenance for a committed TEI, written to a sibling file rather than into the XML.

    Deliberately NOT an XML comment: the regeneration command contains `--` option flags, which XML
    comments may not contain at all, and escaping them would leave an un-pasteable command. Keeping it
    out also means the committed `.tei.xml` stays byte-identical to what GROBID returned, so any future
    diff of it is unambiguously a change in extraction rather than in our own header.
    """
    figures = (
        f"{len(plan.placeholder_figures)} figure(s) replaced by generated placeholders — page layout\n"
        "            differs from the published article, which does not affect reference extraction\n"
        "            (the bibliography is its own trailing section)"
        if plan.placeholder_figures
        else "no figure substitutions"
    )
    params = " ".join(f"{k}={v}" for k, v in sorted(FULLTEXT_PARAMS.items()))
    return (
        f"GENERATED — provenance for {plan.doc.version}.tei.xml. Do not edit either file by hand.\n"
        f"\n"
        f"  source    : tests/documents/{plan.doc.slug}/{plan.doc.version}.tex"
        f" + {plan.doc.version}.bib -> {plan.compiler}/bibtex\n"
        f"  grobid    : {GROBID_IMAGE} (reported version {_grobid_version(tei)})\n"
        f"  endpoint  : {FULLTEXT_ENDPOINT} {params}\n"
        f"  texlive   : {texlive}\n"
        f"  figures   : {figures}\n"
        f"\n"
        f"regenerate:\n"
        f"  uv run python tests/pdf_fixtures.py --slug {plan.doc.slug} "
        f"--version {plan.doc.version} --compile --grobid http://localhost:8070 --write-tei\n"
    )


def _texlive_version() -> str:
    proc = subprocess.run(["pdflatex", "--version"], capture_output=True, text=True, check=False)
    first = proc.stdout.splitlines()[0] if proc.stdout else "unknown"
    return first.strip()


def fixture_tei_path(doc: DocumentVersion) -> Path:
    return FIXTURE_TEI_DIR / doc.slug / f"{doc.version}.tei.xml"


def fixture_provenance_path(doc: DocumentVersion) -> Path:
    return FIXTURE_TEI_DIR / doc.slug / f"{doc.version}.provenance.txt"


def write_tei_fixture(plan: BuildPlan, tei: str) -> Path:
    """Commit the TEI verbatim plus its provenance sibling. Returns the TEI path."""
    if len(tei.encode("utf-8")) > MAX_TEI_BYTES:
        raise RuntimeError(
            f"the TEI for {plan.doc.id} is {len(tei.encode('utf-8')) / 1e6:.1f} MB, over the "
            f"{MAX_TEI_BYTES / 1e6:.1f} MB budget for a committed fixture. Commit a smaller document "
            "instead, or decide deliberately to raise MAX_TEI_BYTES — do not strip <body>, which "
            "carries the in-text citation markers the context tests need."
        )
    target = fixture_tei_path(plan.doc)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(tei, encoding="utf-8")
    fixture_provenance_path(plan.doc).write_text(
        provenance_text(plan, tei, _texlive_version()), encoding="utf-8"
    )
    return target


def tei_fingerprint(tei: str) -> dict[str, object]:
    """Semantic identity of a TEI: what must not change, ignoring byte-level layout noise.

    Byte equality is not portable — font subsetting and hyphenation shift with the TeX Live version,
    and the xelatex path has no reproducible trailer id — so `--check` compares meaning instead.
    """
    from reference_audit.pdf.tei import parse_tei

    refs = parse_tei(tei)
    return {
        "references": len(refs.entries),
        "dois": sorted(e.ids.doi for e in refs.entries if e.ids.doi),
        "linked_keys": sorted(refs.linked_keys()),
        "markers": refs.markers_seen,
    }


# =================================================================================================
# CLI
# =================================================================================================


def _select(args) -> list[DocumentVersion]:
    versions = discover_document_versions()
    if args.all:
        return versions
    chosen = [
        d for d in versions
        if (args.slug is None or d.slug == args.slug)
        and (args.version is None or d.version == args.version)
    ]
    if not chosen:
        raise SystemExit(
            f"no document matched slug={args.slug!r} version={args.version!r}. Available: "
            + ", ".join(d.id for d in versions)
        )
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slug")
    parser.add_argument("--version")
    parser.add_argument("--all", action="store_true", help="every discovered document version")
    parser.add_argument("--preflight-only", action="store_true", help="report inputs, compile nothing")
    parser.add_argument("--compile", action="store_true", help="compile the document to PDF")
    parser.add_argument("--grobid", help="GROBID base URL; fetch the TEI for the compiled PDF")
    parser.add_argument("--write-tei", action="store_true", help="commit the TEI as a test fixture")
    parser.add_argument("--check", action="store_true", help="compare a fresh TEI to the committed one")
    parser.add_argument("--compiler", help="override the LaTeX engine for this run")
    args = parser.parse_args(argv)

    if not (args.all or args.slug or args.version):
        parser.error("give --slug/--version, or --all")

    failures = 0
    for doc in _select(args):
        print(f"\n=== {doc.id} ===")
        try:
            plan = preflight(doc, compiler=args.compiler)
        except MissingDocumentInputsError as exc:
            print(f"  MISSING INPUTS\n{exc}")
            failures += 1
            continue

        print(f"  compiler : {plan.compiler}")
        print(f"  bib stem : {plan.bib_stem}   style: {plan.bib_style}")
        print(f"  build dir: {plan.build_dir}")
        for note in plan.notes:
            print(f"  note: {note}")
        if args.preflight_only:
            continue

        if args.compile:
            try:
                pdf = compile_pdf(plan, quiet=False)
            except LatexCompileError as exc:
                print(f"  COMPILE FAILED\n{exc}")
                failures += 1
                continue
            print(f"  pdf      : {pdf} ({pdf.stat().st_size / 1e6:.2f} MB)")
        else:
            pdf = plan.build_dir / f"{doc.version}.pdf"
            if not pdf.is_file():
                print(f"  no PDF at {pdf} — pass --compile")
                failures += 1
                continue

        if not args.grobid:
            continue
        try:
            tei = fetch_tei(pdf, args.grobid)
        except Exception as exc:  # noqa: BLE001 — a harness: report and continue to the next doc
            print(f"  GROBID FAILED: {type(exc).__name__}: {exc}")
            failures += 1
            continue
        fresh = tei_fingerprint(tei)
        print(f"  tei      : {len(tei) / 1e3:.0f} kB, {fresh['references']} references, "
              f"{fresh['markers']} citation markers")

        if args.check:
            committed = fixture_tei_path(doc)
            if not committed.is_file():
                print(f"  CHECK: no committed fixture at {committed}")
                failures += 1
            else:
                old = tei_fingerprint(committed.read_text(encoding="utf-8"))
                if old == fresh:
                    print("  check    : committed fixture is current")
                else:
                    print("  CHECK FAILED — committed fixture differs semantically:")
                    for key in sorted(fresh):
                        if old.get(key) != fresh[key]:
                            print(f"      {key}: committed={old.get(key)!r} fresh={fresh[key]!r}")
                    failures += 1

        if args.write_tei:
            print(f"  fixture  : {write_tei_fixture(plan, tei)}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
