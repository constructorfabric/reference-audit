# Auditing a PDF

```bash
podman run -d --replace --name grobid -p 8070:8070 docker.io/grobid/grobid:0.9.1-crf   # wait ~30 s
reference-audit audit paper.pdf
```

GROBID supplies both the reference list and the in-text citations, so the audit proceeds exactly as it
would from authored sources. What differs is that the input is now an *extraction*, and the report says
so rather than leaving you to assume otherwise:

- The header is marked `references extracted from PDF`, and an `INPUT NOTES` block lists what the
  extraction could not recover.
- Each entry shows the printed reference number (`[article] b12  (ref #13, cited)`), since the key is a
  GROBID-assigned id and means nothing on its own. A reference GROBID could not title is shown with the
  raw text it read, so you can find it in the PDF.
- Counts a PDF cannot support are not faked. Commented twins and unresolved `\input`s are omitted
  entirely. If GROBID links no in-text citation markers at all, the report says `citedness unknown` and
  lists *nothing* as uncited — rather than claiming every reference is uncited.
- A reference with neither a title nor an identifier is reported as uncheckable and no source is
  queried for it, instead of a title-less search returning an arbitrary paper that could be scored as a
  match.

Failures are named, never degraded into an empty bibliography: an unreachable or failing GROBID, a PDF
with no text layer, and a PDF with no detectable reference list each exit non-zero with the specific
reason (and, for an unreachable service, the command to start one).

## PDFs that embed their references

A PDF made with the [AI4X conference template](https://github.com/kazeevn/AI4X-2027-Template)
carries the BibTeX records of its cited works: the template's bibliography style (`ai4x.bst`, a copy
of `unsrturl.bst` that prints identically) writes each cited entry's record into the `.bbl`, and its
class embeds them as the PDF attachment `cited-references.bib.hex`. Only the cited entries are
embedded, with only the fields printed in the reference list plus identifiers (DOI, ISBN, arXiv id,
PubMed id), `@string` macros and `crossref` already resolved by BibTeX. The records are hex-encoded,
because they pass through TeX on the way to the attachment, where a raw `%`, `#` or `\` would break.

`reference-audit audit paper.pdf` looks for the attachment first. When it is there, the records are
audited exactly like a `.bib` — no GROBID, the same verdict cache, and `--no-network` works:

- The header reads `references embedded in PDF` (under `--no-network`, the usual `parse summary (no
  network)`), and every entry is cited (BibTeX exported only what it printed).
- A record is what the author compiled, not necessarily what is printed — a hand-edited `.bbl` can make
  them differ. Each record's title is therefore looked up in the PDF's printed reference list (after
  its last *References* or *Bibliography* heading; in the whole text if there is neither), and one
  that is not found is an issue on that entry, summarized in
  `INPUT NOTES` and in `summary.titles_not_in_printed_references`.
- The records carry no citing sentences, so `--check-citations` is declared unchecked in `INPUT NOTES`.
  `--ignore-embedded` takes the references, and the citing sentences, from GROBID instead.

A PDF without the attachment goes to GROBID as before. A malformed attachment, or a PDF the attachment
cannot be read from, goes to GROBID too, with the reason in `INPUT NOTES`.
