# Reference Auditor — Product Specification

> **Status:** canonical source specification.
> This is the authoritative, human-authored statement of intent for the project (originally the
> repository `README.md`). The governed SDLC artifacts derive from and elaborate it with
> CPT-ID traceability — see [Traceability](#traceability). When this SPEC and the derived
> artifacts disagree, this SPEC states the *intent* and the artifacts/code state the *current
> realization*; reconcile deliberately.

## Project Goals

The auditor takes as input `.bib` and `.tex` files, and applies a series of checks on them. The
checks are intended to be used:

1. Manually by the paper authors to ensure the absolute best quality of their references.
2. Automatically, to screen submissions for hallucinated references.
3. Automatically by AI agents who are writing papers, to ensure the references are correct and
   up-to-date.

## Audit Steps

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
5. The end result is a very robust and reliable output, one of three:
   1. The `.bib` entry doesn't match a real document.
   2. The `.bib` entry matches exactly one real document.
   3. The `.bib` entry matches multiple real documents.

### 2. Is there a better version of the same artifact?

1. For papers, priority is published > preprint.
2. For books, prefer later editions.

### 3. Produce the absolute best possible reference

1. Compile all available information.
2. Ensure that `.bib` entries use the most correct and canonical format.

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

## Software Design

1. The overall system is based on [Constructor Studio](https://github.com/constructorfabric/studio)
   (used for project governance and traceability; the audit pipeline itself is a plain async Python
   application).
2. The part which queries the databases is modular (easy to change and add adapters for individual
   databases).
3. `.env` contains the API keys.
4. The LLM model is configurable; by default use `gpt-5.4-mini`.
5. Use `uv` to manage Python dependencies.
6. Python code is a module in `src/`, without relative imports and `sys.path.append`.

## Traceability

This SPEC is realized through the governed SDLC artifacts under `architecture/`, which carry
CPT-ID traceability into the code:

- **[PRD.md](PRD.md)** — actors, functional & non-functional requirements, use cases.
- **[DESIGN.md](DESIGN.md)** — components, flows, and how each PRD requirement is met.
- **[DECOMPOSITION.md](DECOMPOSITION.md)** — the feature breakdown.
- **[features/](features/)** — per-feature specifications (CDSL flows + Definitions of Done) that
  map to `@cpt-*` markers in `src/reference_audit/`.

Run `cfs validate` to check artifact structure and code traceability.
