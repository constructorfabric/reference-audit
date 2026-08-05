"""PDF input: GROBID turns a PDF into TEI XML, and a pure mapper turns that TEI into the same
models the `.bib`/`.tex` front end produces.

The split is deliberate and is enforced by `cpt-referenceaudit-constraint-no-network-parse`:

* `pdf.grobid` — the ONLY module here that performs I/O. It talks to one operator-supplied GROBID
  service and nothing else.
* `pdf.tei` — pure. Given the TEI text it returns `BibEntry` and `CitationContext` records, so
  extraction correctness is verifiable offline from recorded TEI, exactly as the source adapters are
  verified from recorded responses.

`parsing` must not import `pdf.grobid`.
"""
