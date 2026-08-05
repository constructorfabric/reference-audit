# Provenance: position-align-ai-to-aspirations

`initial.{tex,bib}` and `polished.{tex,bib}` are the test fixtures and are **authoritative** — the
audit tests pin counts off them (120/144 entries respectively). The two style files exist only so the
fixtures can be compiled to PDF for the PDF-input extraction oracle (`tests/pdf_fixtures.py`).

| Item | Value |
| --- | --- |
| Source | arXiv:2606.13755v2 (`arXiv-2606.13755v2.tar.gz`) |
| Tarball SHA-256 | `4d8a8a35ca4a3b78cba826e96c37f6e8bfff652e16cee4a5ae3ef543ae0d0524` |
| Compiler the authors used | `pdflatex` (per the tarball's `00README.json`) |

## Taken from the tarball

- `icml2026.sty` — required by `\usepackage[accepted]{icml2026}`; not in TeX Live.
- `icml2026.bst` — required by `\bibliographystyle{icml2026}`; not in TeX Live.

Both versions need both files. Neither `.tex` has any `\input` or `\includegraphics`, so these two
files are the complete set of missing inputs for this document.

## Deliberately NOT taken

- **`main.tex`** — byte-identical to `polished.tex`, so it adds nothing, and copying it in would give
  `discover_document_versions()` a phantom third version to parametrize over.
- **`main.bib`** — has the identical 144-key set as `polished.bib` but differs in field formatting.
  The fixture is authoritative; `polished.tex` says `\bibliography{main}`, and the build harness
  stages `polished.bib` under that name inside the gitignored build directory.
- **`algorithm.sty` / `algorithmic.sty`** — already resolvable in TeX Live 2026, and neither `.tex`
  loads them.
