"""PDF input: the printed reference list is read by a language model (default) or by GROBID, and pure
mappers turn either reading into the same models the `.bib`/`.tex` front end produces.

`pdf.llm_refs` holds the language-model reader's schema, its list and DOI checks and the mapping to
`BibEntry`; it is pure, and the call itself is `llm.client.LLMClient.read_pdf`.

The GROBID split is deliberate and is enforced by `cpt-referenceaudit-constraint-no-network-parse`:

* `pdf.grobid` — the ONLY module in this package that performs network I/O. It talks to one
  operator-supplied GROBID service and nothing else.
* `pdf.tei` — pure. Given the TEI text it returns `BibEntry` and `CitationContext` records, so
  extraction correctness is verifiable offline from recorded TEI, exactly as the source adapters are
  verified from recorded responses.

`pdf.embedded` reads a PDF that carries the BibTeX records of its cited works (the AI4X template
makes such PDFs); then neither GROBID nor `pdf.tei` is involved. It reads the local file only.

`parsing` must not import `pdf.grobid`.
"""
