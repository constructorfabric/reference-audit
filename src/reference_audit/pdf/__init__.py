"""PDF input: GROBID turns a PDF into TEI XML, and a pure mapper turns that TEI into the same
models the `.bib`/`.tex` front end produces.

The split is deliberate and is enforced by `cpt-referenceaudit-constraint-no-network-parse`:

* `pdf.grobid` — the ONLY module here that performs network I/O. It talks to one operator-supplied GROBID
  service and nothing else.
* `pdf.tei` — pure. Given the TEI text it returns `BibEntry` and `CitationContext` records, so
  extraction correctness is verifiable offline from recorded TEI, exactly as the source adapters are
  verified from recorded responses.

`pdf.embedded` reads a PDF that carries the BibTeX records of its cited works (the AI4X template
makes such PDFs); then neither GROBID nor `pdf.tei` is involved. It reads the local file only.

`parsing` must not import `pdf.grobid`.
"""
