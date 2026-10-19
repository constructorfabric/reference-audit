# Auditing Word documents

> **WARNING: ONLY WORD'S STRUCTURED BIBLIOGRAPHY IS CHECKED.** Free-form or manually typed
> references are **not checked** and may be missed, even in a document that also has structured
> sources. Zotero/Mendeley fields are not interpreted by this importer. A successful Word audit
> does not establish that every reference visible in the document was checked.

Pass a `.docx` containing sources created with Word's **References → Manage Sources** and
citations inserted with **Insert Citation**:

```bash
reference-audit audit paper.docx
reference-audit audit paper.docx --no-network
reference-audit audit paper.docx --check-citations --format json
```

The AI4X 2027 Word template is supported. From a repository checkout, for example:

```powershell
uv run reference-audit audit "C:\Users\kna.NUSSTF\Documents\GitHub\AI4X-Word-Template\AI4X-2027.docx"
```

Word is the whole input; passing a `.bib` or other second input alongside it is rejected. No Word
installation, document conversion, GROBID service, or LLM is needed to read the bibliography.
The full audit uses the normal database/LLM configuration; `--no-network` reads and reports only.
The cache defaults to `.reference_audit/cache.db` beside the `.docx`.

Every Word invocation prints a prominent warning to **stderr**, including when `--format json` is
used or the document has no supported sources. The same scope warning is included in report
`notes`, so JSON consumers can preserve it without corrupting JSON on stdout.

## What is read

The importer reads native Word bibliography `Sources/Source` XML in `customXml` parts. Source
`Tag` values become entry keys. Personal authors, corporate authors, source types, titles, years,
venues, publishers, pages, volume/issue, editions, DOI, URL, ISBN and marked arXiv identifiers map
to the same entry models and identifier normalization as other input formats. All stored sources
are audited, including sources without an in-text citation. Unknown source types remain unknown.

Native `CITATION` fields provide cited keys and citing sentences for `--check-citations`.
Grouped citations (`\m`), simple fields, and complex fields split across runs are supported.
Main text, tables, text boxes, footnotes, endnotes, headers and footers are read; tracked deletions
are excluded. The cached display of `BIBLIOGRAPHY` fields is excluded from citing contexts.
Bare fields without surrounding prose remain cited but report that their alignment cannot be
checked; an empty context is not sent to the LLM.

If no native citation fields exist, the report says `citedness unknown` and does not list every
source as uncited. Missing source Tags cited by fields are reported; cited sources that failed to
parse appear under `UNPARSEABLE WORD SOURCES` rather than as missing. JSON uses `input_kind="docx"`;
unparsed XML sources have `line=0` because they have no manuscript line number.

## Limits and failures

Free-form reference paragraphs and bibliography field display text are not parsed or checked
against the stored records. Update Word fields before submission; manually editing displayed
references can leave them different from the records actually audited. Third-party citation
manager fields, legacy `.doc`, scanned documents and document conversion are outside this path.
Use a supported `.bib` export or the [PDF path](pdf-input.md) when authored Word records are absent.

A file without supported structured sources fails explicitly, with instructions to supply them
or use another input format. A corrupt package, malformed XML or broken field structure fails the
input rather than returning a partial bibliography. Each malformed source record (including
missing/duplicate Tags, invalid years or unreadable author names) is logged and reported with its
reason; other records continue. If every source is malformed, the report lists every unparsed
source and makes no identification claim. XML parts above 32 MiB and DTD declarations are rejected.

The ordinary per-entry identification failure isolation and cache rules apply. Pipeline version
**0.29** invalidates verdicts from before the Word input path.
