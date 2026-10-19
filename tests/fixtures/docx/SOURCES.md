# AI4X Word fixture provenance

`AI4X-2027.docx` is an unchanged copy of the Office-edited AI4X 2027 Word abstract template
provided by the user, copied on 2026-10-19 from
`C:/Users/kna.NUSSTF/Documents/GitHub/AI4X-Word-Template/AI4X-2027.docx`.

- Source checkout HEAD: `8a261a19c677845a82316210845b467d7f3a08a0`.
- SHA-256 of the actual copied bytes:
  `58087b020fb7fcc82e8dea23416bfd324eea1c808c6fe31800f32ac99ab575e1`.
- The Word repository has no top-level license file. Its README identifies the document as the
  Word version of the [AI4X LaTeX template](https://github.com/kazeevn/AI4X-Abstract-Template).
  `TEMPLATE-LICENSE` preserves that submodule's BSD 2-Clause notice;
  `SOURCE-SERIF-LICENSE.md` preserves the SIL OFL notice for the embedded Source Serif 4 font.
  These notices describe the supplied materials; the fixture was copied at the user's request.

The generated `build_template.py` output does not reproduce this Office-edited document. Keep
the original bytes; changing the fixture requires reviewing its expected records and digest.

`tests/test_docx.py::test_ai4x_word_template_regression` checks all nine source Tags, ten native
citation fields (eleven per-source contexts because one citation groups two sources), DOI/arXiv
identifiers, personal/corporate authors, the complete 34/68/14-author lists, bibliography
bookkeeping, and the offline CLI JSON report plus the prominent scope warning.

The intentionally fabricated committee reference is retained exactly as authored. Offline tests
assert that it is imported, without declaring a network identification verdict for it.
