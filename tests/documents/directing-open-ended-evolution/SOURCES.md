# Provenance: directing-open-ended-evolution

`initial.tex` + `initial.bib` are the test fixture and are **authoritative** — the audit tests pin
counts off them. The remaining `.tex` files exist only so the fixture can be compiled to PDF for the
PDF-input extraction oracle (`tests/pdf_fixtures.py`).

| Item | Value |
| --- | --- |
| Source | arXiv:2606.17091v1 (`arXiv-2606.17091v1.tar.gz`) |
| Tarball SHA-256 | `26f5e7f621a8ead7b3ed47ea88ba55858f6736c7f4fcd6d48abb53022143ca74` |
| Compiler the authors used | `xelatex` (per the tarball's `00README.json`) |

## Taken from the tarball

The six `\input` targets `initial.tex` requires:

`section3.tex`, `section4.tex`, `appendixA_consistency.tex`, `appendixB_protocols.tex`,
`appendixC_optimization_protocols.tex`, `appendixD_render_examples.tex`

## Deliberately NOT taken

- **`main.tex`** — differs from `initial.tex`: the fixture carries copy-editing of the introduction
  and uses `\bibliographystyle{abbrv}` where the tarball uses `plain`. Overwriting the fixture would
  silently change what the audit tests measure.
- **`references.bib`** — byte-identical to `initial.bib`, so it adds nothing. `initial.tex` says
  `\bibliography{references}`; the build harness stages `initial.bib` under that name inside the
  gitignored build directory rather than committing a duplicate.
- **`figures/`** (13 PNGs, 3.0 MB) — irrelevant to reference extraction, which reads the bibliography
  at the end of the document. The harness generates deterministic placeholder images for them and
  says so in the build report and in the TEI provenance header. Committing them would grow the
  repository roughly sevenfold for no test value.

## Verified when these files were added

The six files cite exactly three keys — `fu2023dreamsim`, `kumar2024automating`,
`zhang2018perceptual` — and all three are already present in `initial.bib`. So resolving these
includes leaves `cited_but_missing` empty; it moves those three keys from uncited to cited.
