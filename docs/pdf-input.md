# Auditing a PDF

```bash
reference-audit audit paper.pdf
```

A language model reads the PDF's printed reference list, and each reference is then audited exactly
as one from a `.bib` would be. You need the OpenAI API configured (`openai_api_key`, see
[Configuration](configuration.md)); the PDF is sent to the OpenAI endpoint. To keep it on your machine,
or to get in-text citations, use [GROBID](#with-grobid) instead.

## How the references are read

`gpt-6-luna` (setting `pdf_extraction_model`) gets the PDF, its text layer and page images, and
transcribes every entry of the reference list into a fixed schema: the entry's full printed text
first, then title, authors, year, venue, pages and identifiers read off it. It is told to transcribe
only, never to look up, correct or complete a reference. DOIs, arXiv ids, ISBNs, URLs and years have a
fixed format the model cannot break; a value printed malformed is left empty, with the reason, rather
than repaired into something that looks right.

A reading is used only after these checks:

- **Is the list complete?** Before transcribing, the model names the start of the last printed entry;
  afterwards it says whether it got there. A reading that stops early (a model can return a normal,
  well-formed answer that simply ends partway) is not used.
- **Is every DOI on the page?** A DOI that does not occur in the PDF's text, or that two references with
  different titles both carry, is not trusted as read.

What fails a check goes to `gpt-6.1-sol` (setting `pdf_escalation_model`), a slower, more expensive and
more accurate reader:

- a list that is incomplete, or that both models must agree is absent, is read again as a whole. If
  neither reading can be used, the run stops with both reasons; it never audits a partial list.
- an entry with a suspect DOI is replaced by the second model's reading of it before it is audited.
- after the audit, every entry that ended in **no match**, a **contradiction**, or an **error on its
  title, authors, year or DOI** is compared with the second model's reading of the same printed
  reference. If the second model read it differently, its reading replaces the first and is audited
  again; if it read it the same way, the finding stands and concerns the reference as printed. Entries
  left unresolved because a database failed are not re-read.

The second model reads each PDF at most once, whatever the number of flagged entries. A re-read that
replaced the first reading is noted on its entry, whose findings are then those of the new reading —
from a live run:

```text
[article] r5  (ref #5, citedness unknown)
    Fabrication of Bioinspired Hydrogels: Challenges and Opportunities
    ids: doi:10.1021/acs.macromol.0c00238
    · reading doubt (gpt-6.1-sol): The DOI breaks after 'acs.macro-' before 'mol.0c00238'; the
      line-end hyphen is treated as a word-wrap hyphen and omitted when joining.
    · re-read with gpt-6.1-sol after an error finding on doi in gpt-6-luna's reading: doi: gpt-6-luna
      read '10.1021/acs.macro-mol.0c00238', gpt-6.1-sol read '10.1021/acs.macromol.0c00238'. This is
      gpt-6.1-sol's reading
    ✓ exactly one match (high) — Matched a single work via openalex.
```

A re-read that could not be done, or that found the same reading, is stated as an issue on the entry,
next to the finding it concerns.

Readings that pass their checks are cached by the PDF's content and the model, so a re-run reads
nothing again. The [measurements behind this design](archive/llm-pdf-references.md) put the cost at
about $0.0015 per conference abstract and $0.02 per paper with 130 references with `gpt-6-luna`, at
third-party price listings, plus the second model's reading for documents that need one.

## What the report says

The input is an *extraction*, and the report says so rather than leaving you to assume otherwise:

- The header is marked `references extracted from PDF`, and an `INPUT NOTES` block lists what the
  extraction could not recover.
- Each entry shows its printed position (`[article] r12  (ref #12, citedness unknown)`), since the key
  is assigned by the extraction and means nothing on its own. A reference with no title is shown with
  its printed text, so you can find it in the PDF. Doubts the reader had about an entry (a line break
  inside a DOI, a value left empty because it is printed malformed) are listed on it as reading
  notes; an entry with nothing else to report appears under `FORMATTING NITS & ADVISORIES`.
- The model reads no in-text citations, so citedness is `unknown`: nothing is listed as uncited, and
  `--check-citations` is declared unchecked. GROBID links them.
- A reference with neither a title nor an identifier is reported as uncheckable and no source is
  queried for it, instead of a title-less search returning an arbitrary paper that could be scored as a
  match.
- With `--format json`, each entry's `raw_fields.pdf_read_by` names the model whose reading it is, and
  `summary.pdf_reading` records who read the list, how many entries were re-read or replaced, and the
  tokens used.

## With GROBID

```bash
podman run -d --replace --name grobid -p 8070:8070 docker.io/grobid/grobid:0.9.1-crf   # wait ~30 s
reference-audit audit paper.pdf --pdf-extractor grobid      # or --grobid http://host:8070
```

[GROBID](https://github.com/grobidOrg/grobid) runs locally and supplies both the reference list and the
in-text citations, so the PDF never leaves the machine, citedness is reported, and `--check-citations`
works. It reads references less accurately than the language model, especially author names in
two-column and non-template layouts ([measurements](archive/llm-pdf-references.md)).

- If GROBID links no in-text citation markers at all, the report says `citedness unknown` and lists
  *nothing* as uncited — rather than claiming every reference is uncited.
- Failures are named, never degraded into an empty bibliography: an unreachable or failing GROBID, a PDF
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
audited exactly like a `.bib` — nothing is read from the typeset text, the verdict cache is shared, and
`--no-network` works:

- The header reads `references embedded in PDF` (under `--no-network`, the usual `parse summary (no
  network)`), and every entry is cited (BibTeX exported only what it printed).
- A record is what the author compiled, not necessarily what is printed — a hand-edited `.bbl` can make
  them differ. Each record's title is therefore looked up in the PDF's printed reference list (after
  its last *References* or *Bibliography* heading; in the whole text if there is neither), and one
  that is not found is an issue on that entry, summarized in
  `INPUT NOTES` and in `summary.titles_not_in_printed_references`.
- The records carry no citing sentences, so `--check-citations` is declared unchecked in `INPUT NOTES`.
  `--ignore-embedded --pdf-extractor grobid` takes the references, and the citing sentences, from
  GROBID instead.

A PDF without the attachment is read from its typeset text as above. A malformed attachment, or a PDF
the attachment cannot be read from, is read that way too, with the reason in `INPUT NOTES`.
