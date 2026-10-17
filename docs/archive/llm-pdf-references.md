# LLM reference extraction vs GROBID (2026-10-17)

**Question.** Can an OpenAI model, given the PDF, replace GROBID for extracting the reference list?
We tested `gpt-6.1-sol` and `gpt-6-luna` against GROBID 0.9.1-crf on two kinds of input:

- (a) the compiled TeX→PDF test documents, where the `.bib` is exact ground truth;
- (b) 38 accepted abstracts from [AI4X 2026](https://openreview.net/group?id=AI4X.cc/2026/Conference),
  TeX- and Word-produced, including every abstract that ignored the formatting template.
  For these, ground truth was transcribed by hand.

**Answer.** On reference-list extraction, both models beat GROBID by a wide margin and are close
to exact. On AI4X they recovered 191/191 printed references with no spurious ones (GROBID: 177/191,
14 spurious). Across both test sets, their field errors were 6 transcription slips: 3 wrong DOIs
and 2 dropped authors from luna, 1 wrong DOI from sol. GROBID got the author count wrong for 32 of
175 AI4X references alone. `gpt-6-luna` matches `gpt-6.1-sol` on recall at an
estimated ~1/15 of the cost and half the latency on long bibliographies.

This covers the reference list only. GROBID also links in-text citation markers (citedness and
`--check-citations`), and the models were not asked to do that (see [Open issues](#open-issues)).

**Follow-up: constrained output (schema v2).** Regex and range constraints on identifiers, with a
`null` escape hatch and per-entry doubts, removed every error `gpt-6.1-sol` made: zero field errors
on all 41 PDFs. For `gpt-6-luna`, they fixed the short-document errors, but its long-bibliography
errors persist and vary from run to run. Two deterministic DOI checks catch the most dangerous of
these. See [Constrained output](#constrained-output-schema-v2).

## Setup

- Harness: [`benchmarks/llm_pdf_refs.py`](../../benchmarks/llm_pdf_refs.py).
  - The PDF goes to the Responses API as a base64 `input_file`.
  - The answer is a pydantic `ReferenceList` (structured output).
  - The instructions are "transcribe only what is printed; never look up, correct or complete; a
    field not printed is null".
  - Each reference becomes a `BibEntry` through `parsing.bib.entry_from_fields`, the same seam
    `pdf.tei` uses, so GROBID and the models are scored on identical objects.
  - Default model settings were used (no temperature, no reasoning-effort override), with one run
    per (model, PDF).
- Scoring: the greedy one-to-one matcher of `tests/pdf_oracle.py` (DOI/arXiv first, then title +
  authors, then title alone), plus two tiers for references that print no title: a bare URL, and
  first author + year + first page (chemistry styles).
  - "Spurious" means an extracted reference that matched nothing printed.
  - Field outcomes are counted only over matched pairs: *wrong* (both present, different),
    *missing* (printed, not extracted) and *invented* (extracted, not printed).

### (b) The AI4X sample

OpenReview was accessed read-only: a `GET /notes` for the venue and a `GET /pdf` per note. The venue
has 348 accepted papers, of which 299 are abstracts; the 49 previously published papers were left
out. None of the 299 embeds BibTeX (that template feature is newer), so ground truth had to be built.

The sample was stratified by PDF producer and template use. An abstract counts as *ignoring the
template* when its first page lacks the template's running header ("AI4X – Accelerate, Singapore,
16–19 June 2026").

| stratum | PDFs | producers | printed refs | PDFs without a reference list |
|---|---|---|---|---|
| TeX, template | 11 (random) | pdfTeX | 96 | 0 |
| Word, template | 11 (random) | 9 Word/LibreOffice, 1 macOS Quartz, 1 "Print To PDF" | 59 | 1 |
| no template | 16 (all of them) | 2 pdfTeX, 6 Word, 8 Quartz / Print To PDF | 36 | 10 |
| **total** | **38** | | **191** | **11** |

The no-template group covers numeric, superscript and chemistry styles. Some print no titles at all
("Haber, F. & le Rossignol, Angew. Phys. Chem. 19, 53–72 (1913)"). One reference is a bare URL, and
another has a DOI glued to its article number ("040701DOI 10.1088/…"). The PDFs without a list test
for invented references; one of them has a DOI-like funder id in its acknowledgments.

Ground truth: each PDF was transcribed from the page images, with strings (DOIs, URLs) copied from
the text layer. The rule was what is printed, not what the cited work is. Every disagreement between
an extractor and the ground truth was then checked against the PDF. All four model-vs-ground-truth
disagreements turned out to be model errors. The GROBID disagreements we sampled were GROBID errors.
Ground truth and the models read the same pages, so their agreement is not fully independent
evidence. The annotators were a different model family from the extractors, and every disputed item
was resolved against the page.

## Results

### (a) TeX→PDF roundtrips (truth = the compiled `.bib`)

| document (printed refs) | metric | GROBID | gpt-6.1-sol | gpt-6-luna |
|---|---|---|---|---|
| directing-open-ended-evolution / initial (28), 1 col, `abbrv` | matched / spurious | 28 / 0 | 28 / 0 | 28 / 0 |
| position-align / initial (115), 2 col, `icml2026` | matched / spurious | 107 / 7 | **115 / 0** | **115 / 0** |
| | DOI correct / wrong / unexpected | 54 / 1 / 1 | 56 / 0 / 0 | 56 / 0 / 0 |
| | year wrong / missing | 4 / 40 | 0 / 0 | 0 / 0 |
| | title wrong | 11 | 0 | 0 |
| | printed order kept | 23/107 | 115/115 | 115/115 |
| position-align / polished (134) | matched / spurious | 125 / 12 | **134 / 0** | **134 / 0** |
| | DOI correct / wrong | 78 / 0 | 78 / 0 | 76 / **2** |
| | year wrong / missing | 3 / 34 | 0 / 0 | 0 / 0 |
| | title wrong | 11 | 0 | 0 |

Author-list figures are left out of this table on purpose. Against a `.bib`, they mostly measure the
oracle, not the extractor: `and others` in the `.bib` is printed as "et al.", and "European Parliament
and Council of the European Union" is one printed string but two `.bib` authors. We checked every
author-count disagreement of the models. All of sol's were of this kind. Luna's were too, except one
real error: it dropped "Megan Ma" from a printed list of 40 authors (polished #50).

Luna's two wrong DOIs are both real:

- `10.1207/S15327965PLI1104_01` is printed by the bibliography style as `…PLI1104 01` (TeX turns the
  underscore into a space), with the correct form only in the URL after it. Luna stopped at the
  space.
- A DOI that wraps right after an ISBN (`B978-15586` / `0643-2/50005-6`) got a hyphen inserted
  (`155860-643-2`), copying the ISBN's grouping.

### (b) AI4X abstracts (truth = hand transcription)

| stratum | extractor | recall | spurious | PDFs exact¹ | no-list PDFs left empty |
|---|---|---|---|---|---|
| TeX, template | GROBID | 88/96 (91.7%) | 8 | 6/11 | – |
| | gpt-6.1-sol | 96/96 | 0 | 11/11 | – |
| | gpt-6-luna | 96/96 | 0 | 11/11 | – |
| Word, template | GROBID | 57/59 (96.6%) | 3 | 7/10 | 1/1 |
| | gpt-6.1-sol | 59/59 | 0 | 10/10 | 1/1 |
| | gpt-6-luna | 59/59 | 0 | 10/10 | 1/1 |
| no template | GROBID | 32/36 (88.9%) | 3 | 3/6 | 10/10 |
| | gpt-6.1-sol | 36/36 | 0 | 6/6 | 10/10 |
| | gpt-6-luna | 36/36 | 0 | 6/6 | 10/10 |
| **all** | GROBID | 177/191 (92.7%) | 14 | 16/27 | 11/11 |
| | gpt-6.1-sol | **191/191** | 0 | 27/27 | 11/11 |
| | gpt-6-luna | **191/191** | 0 | 27/27 | 11/11 |

¹ Every printed reference matched and nothing spurious, out of the PDFs that have a list.

Field accuracy over matched pairs (correct / wrong / missing / invented):

| extractor | title | author count | year | DOI | arXiv |
|---|---|---|---|---|---|
| GROBID | 162 / 2 / 1 / 0 | 141 / **32** / 2 / 0 | 166 / 2 / 6 / 1 | 36 / 0 / 2 / 0 | 15 / 0 / 1 / 0 |
| gpt-6.1-sol | 178 / 0 / 0 / 0 | 188 / 0 / 0 / 0 | 186 / 0 / 0 / 0 | 37 / **1** / 0 / 0 | 16 / 0 / 0 / 0 |
| gpt-6-luna | 178 / 0 / 0 / 0 | 187 / **1** / 0 / 0 | 186 / 0 / 0 / 0 | 38 / 0 / 0 / 0 | 16 / 0 / 0 / 0 |

The author count is compared instead of the names. Name formats differ (Vancouver "Chen S",
compound surnames), which makes a name comparison noisy, but a wrong count is always a real error.

What went wrong:

- **GROBID** mostly mis-segments names. The AI4X template's `unsrturl` style prints "Matthew C.
  Fontaine, Julian Togelius, …", and GROBID shifts given names onto the next author ("Matthew, C",
  "Fontaine, Julian", "Togelius, Stefanos", …). In an audit, that produces bogus author-mismatch
  findings. Its misses are mostly references it split in two or cut at a column break, plus two
  chemistry references with no title delimiter. It read one year as 1647 and turned a journal name
  into a title ("Sci Rep").
- **gpt-6.1-sol** kept a line-break hyphen inside a DOI (`acs.macro-mol.0c00238`, printed as
  `acs.macro-` / `mol.0c00238`). It reported the doubt in `problems` ("unclear whether the hyphen is
  part of the DOI … it has been retained").
- **gpt-6-luna** dropped one author ("Peng Wu") from a printed list of 49. Together with the
  roundtrip case (one of 40 dropped), this looks like a pattern on long author lists. In an audit, it would show up as a false "author
  omitted" finding.
- Neither model invented a reference, a DOI, an arXiv id or a year. Neither returned anything for
  the 11 PDFs without a reference list, including the one with the DOI-like funder id.

### Cost and latency

| | AI4X, 38 PDFs: tokens in / out | median / max latency | est. cost per AI4X PDF² | 134-ref paper: out tokens, latency, est. cost² |
|---|---|---|---|---|
| gpt-6.1-sol | 200k / 45k | 18.5 s / 64 s | ~$0.022 | 37k, 472 s, ~$0.43 |
| gpt-6-luna | 200k / 64k (23k reasoning) | 15.3 s / 50 s | ~$0.0014 | 33k, 223 s, ~$0.02 |
| GROBID | – | seconds | local CPU | seconds |

² From third-party listings of OpenAI prices: luna $0.10 / $0.50 per 1M input / output tokens; sol
assumed at gpt-6-sol's $2 / $10, because no first-party price for gpt-6.1-sol was found. Check these
before budgeting.

Output is ~250–280 tokens per reference, about half of it the verbatim `raw_text`. Latency scales
with the output: about 3.5 s per reference for sol and 1.7 s for luna on long lists.

## Constrained output (schema v2)

OpenAI structured outputs enforce a subset of JSON Schema while decoding: `pattern` and `format` on
strings, `minimum`/`maximum` on numbers, `minItems`/`maxItems` on arrays, enums and `const`.

### What constrained decoding does when the value does not fit

Both models accepted every keyword in strict mode and obeyed it. When the true value did not fit,
neither failed. Each emitted a well-formed wrong value instead:

| prompt | constraint | gpt-6-luna | gpt-6.1-sol |
|---|---|---|---|
| a DOI printed as `…PLI1104 01` | DOI pattern | `…PLI1104%2001` | `…PLI1104%2001` |
| "gamma" | enum-like `^(alpha\|beta)$` | `alpha` | `alpha` |
| "June 2024" | `format: date` | `6619-06-01` | `2024-06-01` |
| "ab" | `minLength: 10` | garbage filler | a long unrelated text |

A constraint guarantees the shape of a value, not that it was printed. Without an escape hatch it
turns a mismatch into silent corruption, which is the failure mode this project is built to
avoid. With a nullable field and an instruction to use `null` rather than repair, both models
returned `null` and explained why for a space in a DOI and for a letter O in place of a zero. Given
both a malformed `doi:` field and a well-formed doi.org URL in the same entry, both took the URL
form.

### The v2 schema

In `benchmarks/llm_pdf_refs.py`, `ReferenceListV2` differs from v1 as follows:

- `doi`, `arxiv_id`, `isbn` and `url` carry regex patterns, and `year` is bounded to 1000–2999. All
  of them are nullable, and the instructions say "if what is printed does not fit, null plus a
  doubt; never repair".
- Each entry has `doubts`: judgments that could change a value (a line break inside an identifier
  or name, a value left null, an unclear boundary). The first version also logged ordinary word
  hyphenation, which gave sol 107 doubts on AI4X, so the instruction now excludes it.
- `raw_text` comes first, because output follows schema key order and the fields are then read off
  the transcription.
- `printed_author_count` before `authors` was tried and removed. On long lists it disagreed with
  correct lists 3 times and agreed with a list that had lost a name, so it caught nothing.

### Results, v1 vs v2

| | AI4X field errors (191 refs) | roundtrip field errors (277 refs) | doubts raised (AI4X) | AI4X tokens out | AI4X median latency |
|---|---|---|---|---|---|
| gpt-6.1-sol v1 | 1 (kept line-break hyphen in a DOI) | 0 | – | 45k | 18.5 s |
| gpt-6.1-sol v2 | **0** | **0** | 47 | 48k | 30.0 s |
| gpt-6-luna v1 | 1 (dropped 1 of 49 authors) | 2 wrong DOIs | – | 64k | 15.3 s |
| gpt-6-luna v2 | **0** | 2 wrong DOIs, 1 wrong year, 2 dropped authors | 18 | 68k | 17.0 s |

Recall was unchanged: 191/191 and 277/277 with no spurious references for every model and schema.
The v2 doubts sit where the hard calls are, for example the `acs.macro-` / `mol` DOI that sol got
wrong under v1. Sol also left one DOI `null` with a doubt where the bibliography prints a real space
inside it (`findings- acl.847`), which is the intended behavior.

Luna's v2 errors are all on the two long documents, and they differ between runs. An earlier v2
draft run had none of the DOI errors, but had the same dropped author. It also dropped 1 of 63
authors on the 115-reference document (#82). That drop was hidden in the aggregate count among the
oracle artifacts; the per-run scorer (`runs`), which filters those artifacts, exposed it.

- It inserted a hyphen into a DOI that wraps next to an ISBN (the same error as in v1).
- It dropped "Nicholas Schiefer" from a list of 63 authors. The name is missing from its `raw_text`
  too, so the transcription itself skipped it.
- It transcribed #73 (Kross et al., PLoS ONE 2013) with the venue, year and DOI of #101 (Primack et
  al., 2017) spliced into its `raw_text`. The DOI is well-formed and printed in the document, so no
  schema constraint can catch it.

### Deterministic DOI checks

These checks don't involve the model, and both scorers now run them:

- **DOI on two entries with different titles** (`shared_dois`) flagged luna's splice and GROBID's
  known cross-reference bleed (`pew2023climate`). It also flagged one AI4X abstract (Nz5EQhs0ac)
  that really prints one DOI under two different titles. That one is a true finding about the paper,
  reported by every extractor and present in the ground truth. There were no other flags.
- **DOI absent from the PDF's text layer** (`doi_guard`) caught luna's ISBN-hyphen DOI. It cannot
  settle hyphen questions in general, though. On AI4X it also flagged four correct DOIs, because
  `pdftotext` loses hyphens that the page image shows (`s41467 -024 50024 -6`). A production version
  should treat "absent even ignoring hyphens" as an error and a hyphen-only difference as a doubt.

## Reasoning effort (gpt-6-luna)

Can more reasoning make luna catch its remaining errors? We ran it 3 times at each effort it
accepts above the default (`medium`), with the v2 schema, on the two long roundtrip documents where
all its remaining errors are (`extract --model gpt-6-luna:<effort> --repeat 3`, scored with `runs`).
`minimal` is rejected; `none` and `low` are below the default and were not tested.

| effort | errors per run, 115 refs | errors per run, 134 refs | total (6 runs) | output tokens per run | latency per run |
|---|---|---|---|---|---|
| medium (default) | 1, 0, 0 | 4, **truncated**, 1 | 6 + 1 truncation | 24–34k | 3–4 min |
| high | 1, 1, 0 | 1, 1, 0 | 4 | 26–34k | ~3 min |
| xhigh | 1, 0, 2 | 3, 3, 1 | 10 | 26–35k | 3–4 min |
| max | 0, 0, 0 | 1, 0, 3 | 4 | 55–71k (24–46k reasoning) | 4.5–6 min |

**No.** More effort does not remove the errors. Run-to-run variance is larger than any effort
effect. The same error types appear at every level:

- dropped or duplicated authors in 40–63-author lists, at every effort;
- the #73 splice of another entry's DOI, at medium, xhigh and max;
- the hyphen inserted into a DOI next to an ISBN, at medium, high and xhigh.

`max` costs about twice the output tokens and 1.5× the latency. In one run it also produced the only
DOI in this whole study that occurs nowhere in the document (`10.1016/j.amepre.2013.01.006` for
#73): a more confident recall from memory, which is exactly what the instructions forbid.

Two findings matter more than effort:

- **Silent truncation.** At the default effort, one run stopped after 30 of 134 references. The API
  reported a normal completed response, and the schema was satisfied. The only sign was a sentence
  in `problems` ("the supplied response is incomplete"). Counting against the highest printed label
  cannot catch this for author-year styles, which print no labels. The schema needs a structured
  `complete` flag that code can gate on, instead of free text.
- **The DOI checks caught every DOI error at every effort.** That is 8 of 8: the ISBN-hyphen and
  invented DOIs by `doi_guard`, the splices by `shared_dois`. The wrong year on #73 came with a
  flagged splice. What no check catches is author-list errors (about one per long-document run) and
  the truncation.

gpt-6.1-sol at default effort had no errors on either long document in any run (3 runs per
document, across v1, the v2 draft and v2).

## Adopted as the default extractor (schema v3)

Following this study, `gpt-6-luna` became the default PDF extractor, with `gpt-6.1-sol` as the
escalation model. Re-reads are triggered by the list checks, by the DOI checks, and by entries whose
audit ends in 'no match', a contradiction or an error on title, authors, year or DOI. The current
behavior is documented in [Auditing a PDF](../pdf-input.md).

The product schema (`pdf/llm_refs.py`, `v3` in the harness) is v2 plus the completeness fields
proposed above: `last_printed_entry_start` named before the list and `coverage` reported after it.
One run of each model over the 41 benchmark PDFs:

- **Field accuracy:** unchanged from v2. Both models recovered 191/191 AI4X references with no field
  errors, apart from luna's IEC-standard author, a judgment call.
- **No false rejections.** `list_problems` accepted all 41 of sol's lists and 40 of luna's.
- **The one rejection was real.** Luna stopped the 134-reference document after 27 entries, inside a
  completed response, as it had once in the effort sweep. This time both new signals caught it: the
  model reported `stopped_early`, and its last entry was not the last printed entry it had named
  ("Zhi-Xuan, T., Carroll, M., …"). In the product, that list goes to sol.

## Open issues

These would have to be solved before the models could replace GROBID in the product.

- **Citation contexts.** The PDF path gets in-text marker linking from GROBID (citedness and
  `--check-citations`). This experiment did not test it. Options are a second LLM task, or GROBID for
  contexts with the LLM's list for entries.
- **Long bibliographies.** A single call took 4–8 minutes for 134 references. A review with
  several hundred references could hit the output-token limit. The harness treats an incomplete
  response as a failure, never as a partial list. A production version would need chunking (by page)
  and a completeness check, such as the extracted count against the highest printed label.
- **DOI transcription** is the only dangerous error class observed: a wrong DOI points the auditor at
  a real, different paper. Schema v2 removes the malformed-DOI cases, and the two
  [deterministic checks](#deterministic-doi-checks) catch the splice and the ISBN-hyphen case. A
  mis-joined DOI that happens to be well-formed and present nowhere else would still pass both.
- **Dropped authors in long author lists** (luna only, at every reasoning effort; see
  [Reasoning effort](#reasoning-effort-gpt-6-luna)) are not visible in the output. Catching them
  would mean comparing `raw_text` with the PDF's text layer.
- **Truncation without an error.** A response can be complete as far as the API is concerned but
  stop early. This is resolved in the product by the v3 completeness fields; see
  [Adopted as the default extractor](#adopted-as-the-default-extractor-schema-v3).
- **Privacy.** GROBID runs locally. This sends the whole manuscript to OpenAI, which matters for
  screening unpublished submissions.
- **Statistical power.** 3 roundtrip documents and 38 abstracts (191 references), with one run per
  model. Run-to-run variance of the models was not measured.

## Reproducing

```bash
uv run python benchmarks/llm_pdf_refs.py roundtrip-pdfs        # links tests/documents/*/.build PDFs
uv run python benchmarks/llm_pdf_refs.py extract --schema v1 benchmarks/runs/llm_pdf_refs/roundtrip_pdfs/*.pdf <ai4x pdfs>
uv run python benchmarks/llm_pdf_refs.py extract --schema v2 benchmarks/runs/llm_pdf_refs/roundtrip_pdfs/*.pdf <ai4x pdfs>
uv run python benchmarks/llm_pdf_refs.py roundtrip
uv run python benchmarks/llm_pdf_refs.py grobid <ai4x pdfs>      # needs a running GROBID
uv run python benchmarks/llm_pdf_refs.py ai4x --sample <sample.json> --gt <gt dir> --pdf-dir <ai4x pdf dir>
# reasoning effort: 3 runs per effort on the long roundtrips, one line per run
uv run python benchmarks/llm_pdf_refs.py extract --repeat 3 --model gpt-6-luna --model gpt-6-luna:high \
    --model gpt-6-luna:xhigh --model gpt-6-luna:max benchmarks/runs/llm_pdf_refs/roundtrip_pdfs/position-align-*.pdf
uv run python benchmarks/llm_pdf_refs.py runs --model gpt-6-luna --model gpt-6-luna:high --model gpt-6-luna:xhigh --model gpt-6-luna:max
```

The run outputs, the sample list and the ground truth were kept under the git-ignored
`benchmarks/runs/llm_pdf_refs/` (`ai4x/sample.json`, `ai4x/gt/*.json`, `ai4x/score.txt`,
`roundtrip.txt`). The AI4X PDFs themselves are not redistributed.

Each ground-truth file has the form `{"pdf", "has_reference_list", "reference_style", "references":
[{"label", "raw", "title", "authors", "authors_truncated", "year", "venue", "volume", "pages", "doi",
"arxiv_id", "url"}], "inline_citations", "notes"}`. Every field is as printed and null when not printed.
