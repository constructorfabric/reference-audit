# Reference Auditor — Product Specification

> **Status:** canonical source specification.
> This is the authoritative, human-authored statement of intent for the project (originally the
> repository `README.md`). The governed SDLC artifacts derive from and elaborate it with
> CPT-ID traceability — see [Traceability](#traceability). When this SPEC and the derived
> artifacts disagree, this SPEC states the *intent* and the artifacts/code state the *current
> realization*; reconcile deliberately.

## Project Goals

The auditor takes `.bib` (optionally with `.tex`), PDF, or Word `.docx` with native structured
bibliography sources, and applies a series of checks on them. The
checks are intended to be used:

1. Manually by the paper authors to ensure the absolute best quality of their references.
2. Automatically, to screen submissions for hallucinated references.
3. Automatically by AI agents who are writing papers, to ensure the references are correct and
   up-to-date.

## Audit Steps

The audited input is either a `.bib` bibliography with the `.tex` that cites it, or the paper's PDF on
its own — for mass automated processing the PDF is usually all that exists. A PDF is converted to the
same reference and citation records the authored sources produce, so every step below is identical
regardless of which arrived. A Word `.docx` is also a standalone input: authored native Word
bibliography sources and `CITATION` fields supply the records and contexts offline.

**Word scope warning:** every Word audit MUST prominently warn that only the structured
bibliography is checked. Free-form / manually typed references are not checked and may be missed,
even when structured records are present. The warning MUST go to stderr and report notes so JSON
stdout remains valid. No structured sources is an explicit input failure; malformed individual
sources are logged and reported independently. Formatted bibliography display text and third-party
citation manager fields are outside the Word importer. See [Word input](../docs/word-input.md).

### 1. Which exact artifact does the reference point to?

- For papers we want to find a **DOI** — but also keep a list of reputable venues which don't
  assign one, e.g. TMLR.
- For books, we want to find an **ISBN**.
- For books, we prefer chapter citation, but don't insist.
- For artifacts, we want a **URL**.

The general plan is:

1. Query the databases.
2. Apply formal, code-based filters (where we are able to derive them).
3. Unless a returned record is a 100% match, use an LLM to filter the results one-by-one —
   "can the returned record correspond to the entry in the `.bib`?"
4. If there are multiple plausible records, use formal, code-based filters (where we are able to
   derive them) to check whether they correspond to the same object — again using an LLM as the
   final step.
5. The end result is a very robust and reliable output, one of four:
   1. The `.bib` entry doesn't match a real document.
   2. The `.bib` entry matches exactly one real document.
   3. The `.bib` entry has multiple accepted candidate groups after same-work clustering.
   4. A hard paper identifier resolves to a real paper, but supplied citation fields contradict
      the resolved paper: `contradiction`. The ID target and conflict evidence must be retained.

A contradiction requires a successful by-ID record carrying the cited DOI, arXiv ID, PMID, bibcode
or OpenAlex Work ID (where supported), and a definitive field error. It takes precedence over
metadata-based matches, no-match and ambiguity. Missing/placeholder fields, cosmetic variants, uncertain
judgments, outages, and DOI registration without paper metadata do not prove contradiction.
An omitted co-author is a field-completeness issue, not an identity contradiction, regardless of
the optional partial-author severity policy. Confirmed related versions are compared against the version cited. Inconclusive LLM comparisons
and failed ID lookups are reported and retried when no other field definitively establishes
contradiction, never memoized as a settled consistency result.
This identity consistency check runs even if optional field reporting is disabled. Books and
chapters keep their edition-aware identification policy.

### 2. Is there a better version of the same artifact?

1. For papers, priority is published > preprint.
2. For books, prefer later editions.

### 3. Produce the absolute best possible reference

1. Compile all available information.
2. Ensure that `.bib` entries use the most correct and canonical format.

### 4. Is the citation used faithfully?

For each place a reference is cited, check that the **reason it is cited** (the claim the citing
sentence attaches to it) is consistent with what the cited work actually claims — compared, in this
first version, against the cited work's **abstract**. This is an advisory check: it flags a citation
that is *contradicted* by the cited work, and reports honestly when the abstract is silent or
unavailable (never inferring misuse from absence of evidence). It never changes the identification
verdict of step 1.

## General Design Principles

1. Databases might be incomplete and metadata can sometimes be incorrect (e.g. month). DOI & ISBN
   uniquely identify documents; title & author list might be spelled slightly differently but are
   usually correct.
2. *Modus operandi*: the system processes cases. It will make mistakes (and be corrected). The goal
   is to use individual failures to improve the system's overall reliability. Document the quirks of
   the databases and data you encounter.
3. When fixing a mistake, add it as a unit test. Unit tests should use mocked DB responses.
4. Maintain a DB of checks so repeated runs on the same `.bib` file won't trigger unnecessary LLM &
   DB calls.

Regression coverage includes copied HALLMARK v1.2 `dev_public` and `test_public` data with pinned
provenance, attribution and license, without a HALLMARK dependency. Binary benchmark labels are
diagnostic only; native expectations require independent evidence. Report malformed, ambiguous,
failed and uncheckable records independently, with reasons, rather than force binary agreement.
All records exercise offline parsing; reviewed cases use recorded independent source metadata,
and full-pipeline contract checks are explicitly opt-in. See `tests/fixtures/hallmark/v1.2/README.md`. This test infrastructure
is implemented but not separately `@cpt`-traced.

A venue error must be grounded in the specific cited published artifact or absence from a
demonstrably complete catalogue covering the claimed venue/year/track. Another version's venue,
an arXiv DOI, search silence or an inaccessible event website cannot establish non-publication.
When that evidence is unavailable, retain the identified work and report the venue as unverified.

## Software Design

1. The overall system is based on [Constructor Studio](https://github.com/constructorfabric/studio)
   (used for project governance and traceability; the audit pipeline itself is a plain async Python
   application).
2. The part which queries the databases is modular (easy to change and add adapters for individual
   databases).
3. API keys and other settings live outside the code: the per-user TOML file
   `~/.config/reference-audit/config.toml` (`$XDG_CONFIG_HOME` respected), so an installed package
   works from any directory, overridden by `.env` in the current directory, overridden by environment
   variables. Unknown keys in the TOML file are an error. Implemented in `config.py`
   (`user_config_file()`); not separately `@cpt`-traced.
4. The LLM model is configurable; by default use `gpt-6-luna`. The sampling temperature is the
   model default unless `LLM_TEMPERATURE` is set (gpt-6-luna accepts only its default).
5. Use `uv` to manage Python dependencies.
6. Python code is a module in `src/`, without relative imports and `sys.path.append`.
7. PDF input is read by a language model by default (`gpt-6-luna`, configurable): it transcribes the
   printed reference list into a constrained schema, and a stronger model (`gpt-6.1-sol`) re-reads a
   list that is incomplete by its own account, DOIs absent from the PDF's text, and entries whose
   audit ends in 'no match', a contradiction or an error a misreading can cause. This sends the PDF to
   the OpenAI API and gives no in-text citations. Alternatively (`--pdf-extractor grobid`), a
   locally-run GROBID extracts the list and the in-text citations; the auditor talks to it but does not
   manage it. Either way the network call and the mapping to records are separate modules, so the
   mapping is verifiable offline. Extraction quality is measured against test documents compiled from
   their own `.bib`, which makes that `.bib` ground truth, and against hand-transcribed third-party
   PDFs. A PDF that embeds the BibTeX records of its cited works (the AI4X template's) is read from
   them instead.

## Traceability

This SPEC is realized through the governed SDLC artifacts under `architecture/`, which carry
CPT-ID traceability into the code:

- **[PRD.md](PRD.md)** — actors, functional & non-functional requirements, use cases.
- **[DESIGN.md](DESIGN.md)** — components, flows, and how each PRD requirement is met.
- **[DECOMPOSITION.md](DECOMPOSITION.md)** — the feature breakdown.
- **[features/](features/)** — per-feature specifications (CDSL flows + Definitions of Done) that
  map to `@cpt-*` markers in `src/reference_audit/`.

Run `cfs validate` to check artifact structure and code traceability.
