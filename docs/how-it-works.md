# How it works

Word `.docx` input reads native bibliography source records and `CITATION` fields into the same
entry/context models before the identification funnel. **Only the structured bibliography is
checked; free-form references are not checked and may be missed.** Every Word run prints this
warning and retains it in report notes. See [Word input](word-input.md). Pipeline **0.29** adds
this input path and invalidates earlier whole-entry verdicts.

For each entry the tool runs a funnel that prefers cheap, deterministic evidence and only escalates
to an LLM when needed:

1. **Parse** the input — either the `.bib` (resolving `\cite`/`\nocite` in the `.tex`, when one is given), or a PDF,
   whose printed reference list a language model reads (or, with `--pdf-extractor grobid`, from which
   a locally-run [GROBID](https://github.com/grobidOrg/grobid) extracts both the reference list and the
   in-text citation markers). A PDF that embeds the BibTeX records of its cited works (as one made
   with the [AI4X template](pdf-input.md#pdfs-that-embed-their-references) does) is read from those
   records instead. Pipeline **0.31** makes the language model the default PDF reader and adds its
   re-read of flagged entries, invalidating earlier whole-entry verdicts. Either way, DOIs, ISBNs, arXiv ids, OpenAlex Work
   ids (an `openalex.org/W…` URL becomes a first-class identifier) and Google Books volume ids (a
   `books.google.…/books?id=…` URL) are normalized identically, and everything below is unchanged.
   An entry with no `url` field takes its URL from `howpublished` or `note`, where `@misc` entries
   conventionally keep it (`howpublished = {\url{…}}`); a `howpublished` that is only a URL is not
   read as the venue.
   HTML character references left by web-scraped metadata (`d&apos;Amore`, `&amp;`) are decoded in
   the `.bib`'s title, author, venue, publisher and pages, and in every source record's title,
   authors, venue and publisher, so the two compare as plain text in identification and in the field
   check of step 7, and findings report the decoded value.
2. **Query** multiple scholarly databases — Crossref, OpenAlex, Semantic Scholar, arXiv, DBLP, Open
   Library, Google Books — by identifier where the source supports it, and by title/author. The
   title/author search is routed by entry type: a `@book`/`@incollection` goes to Open Library,
   Google Books and Crossref; a `@misc` also to arXiv; anything else to Crossref, OpenAlex, Semantic
   Scholar and DBLP. With the [ClickHouse backend](clickhouse-backend.md), Semantic Scholar,
   OpenAlex and DBLP are read from a local mirror instead. `?` and `*` are dropped from an OpenAlex
   title search, which reads them as wildcards and rejects them. A cited Google Books volume id is
   resolved directly to that volume, and a cited OpenAlex Work id to that Work when the entry
   carries no DOI or arXiv id (OpenAlex is queried by those first) — the authoritative key for
   entries, notably trade books, the other sources miss. DBLP is the authority for the premier
   CS/ML venues (NeurIPS, ICLR, ICML/PMLR, TMLR), which mint no DOI and are thinly covered by the
   article-centric sources. A paper cited only by its proceedings or OpenReview URL is confirmed by
   a near-exact title and a strong author overlap with a record (a bare URL is not treated as a
   matching anchor, so such an entry takes the same strict title+author path as one with no
   identifier at all). DBLP is queried through its SPARQL endpoint (`sparql.dblp.org`), because its
   search API now serves automated clients a bot-challenge page, never JSON. A truncated author
   list (the BibTeX `and others` / "et al." convention) is recognized as such, so its named authors
   all appearing in the full record's author list (in any order) confirms the entry rather than
   reading the omitted names as a different work. The **publisher** of record (the DOI landing
   page's BibTeX export, for MIT Press / Silverchair and SAGE / Atypon; for a paper cited by its
   NeurIPS proceedings URL, that paper's official `-Bibtex.bib` export) is not an identity source: it
   is queried only after the verdict, to enrich the matched work's venue, volume, issue, pages,
   publisher and year. A NeurIPS export joins the matched work only when its title and authors tie
   it to that work; one that names another paper, or a proceedings URL with no export, is reported.
   It is what verifies a NeurIPS paper's `publisher = {Curran Associates, Inc.}`, which no
   aggregator carries, and it supplies the Curran DOI NeurIPS has minted since 2022.
3. **Pool** the results, merging records that are the same work (shared identifier, or a
   preprint↔published version link). Pooling keys on a record's canonical ISBN-13 only, so a book
   chapter that inherits its volume's ISBNs is not fused with its sibling chapters. When the
   accepted candidates are counted as works, ISBNs are treated as a *set*: one book registers several
   ISBN-13s (print + electronic, per edition), so records that share any one of them are the same
   work rather than different books. A version relation (a matching title, or a
   database's version link) joins two groups only when every record of one has authors compatible
   with every record of the other, compared person by person. So a conference paper is not fused with
   a journal extension that has other authors (CrossFormer at ICLR, CrossFormer++ in TPAMI), not even
   through a one-author supplementary-material DOI that fits both. A pooled record keeps its member
   records, and its venue is never a preprint server (`arXiv`, DBLP's `CoRR`) while a member names
   the journal or conference. Its author list, which scoring and the LLM see, comes from the most
   reliable source that has one (the publisher, then DBLP, Crossref, arXiv, OpenAlex, Semantic
   Scholar), not from the citation-richest record.
4. **Score** each candidate with interpretable features (title/author/year/venue similarity,
   identifier agreement, and distinct-work signals). Identifier agreement is likewise set-aware for
   ISBNs — a cite that gives a book's electronic ISBN matches a source record carrying that book's
   print ISBN, and it counts as a *conflict* only when the two ISBN sets are wholly disjoint (this is
   what lets a book-chapter cited by its volume's ISBN resolve deterministically instead of falling
   to the LLM). Author similarity compares surnames, with two exceptions. A compound surname
   matches any of its parts (`Sauceda Felix` / `Sauceda`), since databases often keep only the
   first. An organizational author (`AI4X Program Committee`, `The ATLAS Collaboration`) is
   compared by its distinctive words and never matches a person, so a shared generic noun
   (`Committee`) cannot make an invented reference look like a real document by another body.
   Pipeline **0.30** invalidates verdicts scored before these rules.
5. **Adjudicate** anything the formal rules leave undecided with an LLM, asking one record at a time
   whether it can correspond to the entry; a second LLM check decides whether two strong candidates
   are the *same* work. At most `llm_max_candidates` (8) candidates, the best-scoring, are
   adjudicated per entry; an entry with more is left unresolved and says so.
6. **Verdict** — check resolved hard paper identifiers for contradictions, then count the accepted
   work groups and report none / exactly one / multiple / contradiction. An entry left
   without a verdict says why: the source that failed, the LLM call that failed, or the candidates
   that stayed undecided.
   The ID consistency check uses successful by-ID records before pooling: DOI (including arXiv
   DOIs), arXiv ID, PMID, bibcode, and OpenAlex Work ID, where an adapter supports resolution.
   Confirmed errors in supplied title, authors, year, venue, volume, issue, pages or publisher yield
   `contradiction`, even with `CHECK_FIELDS=false`. Missing/placeholder values, cosmetic differences, uncertain
   judgments and lookup failures do not establish a contradiction. Inconclusive LLM comparisons
   and failed ID lookups are reported and prevent caching an unproven consistency result, so the
   next run retries them. A contradiction established by another definitive field error is conclusive. A bare URL is excluded; books and
   chapters retain their edition-aware policy. DOI registration alone is insufficient.
   Confirmed related versions are compared against the cited version; with no explicit venue,
   a published DOI takes precedence over an accompanying arXiv ID. A PMID is queried through
   Semantic Scholar; no bundled adapter resolves bibcodes.
   Pipeline **0.28** invalidates older cached verdicts; cached contradictions retain their field evidence.
   Omitting co-authors remains an ordinary field finding under `PARTIAL_AUTHORS`; it does not
   contradict the ID when all named authors belong to its paper.
   Entry hashes include all verdict-producing citation fields (including pages, publisher, volume,
   issue and entry type), so correcting a conflict forces a new verdict.
7. **Check the fields** of an exactly-one match (title, authors, venue, year, volume, pages, …).
   - Each field is compared against the **version the entry cites**, read from the pooled record's
     members: a citation of the arXiv preprint against the preprint, one of a journal or conference
     against the published paper, and of a conference paper and its later journal version, the one
     from the cited year. Within that version the value most sources agree on wins, so one source's
     defect (OpenAlex's corrupted title for Constitutional AI) is outvoted.
   - **Authors** are checked person by person against every source's author list, so name order
     (`Tian Li` / `Li Tian`), compound surnames, diminutives (`Tim` / `Timothy`) and one source's
     defect do not flag a real author, and a near-namesake does not pass (`Carreira` is not
     `Barreira`). A cited author on no source is an
     `error`. A citation that names only some of the work's authors without `and others` is reported
     per `--partial-authors`: `ignore`, `warn` (an `uncertain` finding, the default) or `error`.
   - A difference no rule settles goes to the LLM, which is shown the matched work as the database
     records it, never the entry under review. A substituted title word is an error. A different
     venue is an error only with evidence tied to the cited published artifact or a complete
     catalogue covering the claimed venue, year and track. Another version's publication venue,
     an arXiv DOI, an unsuccessful search or a missing website cannot establish non-publication;
     inconclusive claims stay `uncertain` / `unverifiable`, with the reason reported.
   - A publisher or Crossref record for the **cited published DOI** supplies positive venue
     evidence and takes precedence over other versions' venue values. JSON venue errors carry
     `venue_evidence` (source URLs and the bounded scope). Source adapters may supply typed
     `VenueCatalogue` inventories with an authority-declared total and coverage URL; incomplete,
     wrong-year or ambiguous-track inventories and possible identity/title/author matches cannot
     establish absence. Existing top-k adapters do not supply complete inventories; automatic
     exhaustive proceedings retrieval is not implemented. These checks never change `exactly_one`.
   - The **cited DOI** is checked too (except for a book, which is legitimately cited by a chapter
     DOI, and an arXiv DataCite DOI): when the matched work does not carry it, the DOI's own records
     are looked up. A DOI that belongs to a different paper, or that the doi.org Handle API does not
     know, is reported as a wrong `doi` field, naming that paper. A DOI found for backfill is checked
     at doi.org too: one that does not resolve is reported as unusable, not as a find. An arXiv
     DataCite DOI (`10.48550/arXiv.…`) is not reported as a find for an entry that already cites
     that arXiv id (it is the same identifier) or for a paper at a venue that mints no DOI (NeurIPS,
     ICLR, ICML/PMLR, JMLR, TMLR), since it is the preprint's DOI, not the cited version's. For such
     a venue the parse-time `no DOI/arXiv id` warning is withdrawn when identification finds no
     published DOI; a published DOI it does find (NeurIPS since 2022) is still reported.
8. **Suggest a better version** of a matched work: the published version of a cited preprint, a
   newer arXiv version, or a later edition of a cited book.

A reference identified only by a URL (a `@misc` blog post, software or project page that no scholarly
database indexes) is verified against the page itself: the tool fetches the URL, checks the page's
own HTML metadata (Open Graph / Twitter card / `citation_*` / Dublin Core / `<title>` + authors)
against the citation, and falls back to the LLM when that metadata is missing or inconclusive. A dead
link or a page that cannot be confirmed is reported and left unresolved, never silently passed. Only
a high-confidence LLM judgment that the live page is a *different* resource gives *no match* (a
possibly wrong or fabricated URL).

Many such pages are **JavaScript single-page apps** (e.g. `data.europa.eu` dataset pages): a plain
HTTP fetch returns only an empty app shell, with the real title/text injected after the page's
JavaScript runs. The tool detects these shells and re-fetches them through a headless browser
(Chromium via `--dump-dom`) so the rendered page is judged like any other. This is the reliability
fix for a class of false hallucinations: an unreadable shell is *never* read as "a different page".
If no headless browser is available, or rendering fails, the entry is reported and left unresolved
(retried next run) rather than flagged. Rendering is on by default (`web_render_enabled`); point
`WEB_RENDER_BROWSER_PATH` at a Chrome/Chromium binary if one is not auto-detected on `PATH`.

A reference that cites an **OpenAlex Work id** (an `openalex.org/W…` URL, common for books and other
titles the article-centric search and Crossref/Open Library miss) is resolved directly to that Work,
also when the entry carries a DOI or arXiv id (OpenAlex is then queried by both).
When the Work matches the entry's title and authors it is pinned as the match — the author-supplied
Work id is treated as authoritative identity, so a similar-titled foreign-DOI record is never merged
in and its identifiers are never backfilled onto the entry. A cited DOI that belongs to another work
is then reported by the cited-DOI check (a `doi` field finding naming that work). A cited Work id
whose Work has a mismatched title/author does not confirm the entry; the entry gets whatever verdict
the other candidates support, and an issue names the work the id resolves to. A Work id that resolves
to nothing, or whose lookup failed, is reported too. These Work-id issues are reported for every
verdict, including an unresolved entry.

For **books**, Open Library is the authority of record for identity: it is edition-aware, so it can
confirm a real book the article-centric matcher rejected (e.g. a 1976 original whose only DOI-bearing
candidate is a 2018 reprint). Because of this, a book is only ever called a hallucination when that
authority was actually consulted — if Open Library is unreachable, an article-search "no match" is
**not** trusted: the entry is left unresolved (reported, retried next run) rather than flagged as a
likely hallucination we never really checked.

A book is frequently cited by a **chapter-level DOI** (Oxford Scholarship Online and similar mint one
DOI per chapter, e.g. `10.1093/{isbn}.003.0002`), which resolves to a *component* whose title differs
from the book — so the article-centric matcher rejects it as a non-match. When such a book carries no
ISBN of its own, the ISBNs on that cited DOI's own record are used to locate the book in Open Library
(every ISBN is tried, since one book registers several and Open Library indexes only some). The cited
*edition* is still pinned by the entry's own year/publisher, so a book cited by a chapter DOI grounds
on the edition the author actually cited (the 1989 original, not a 1992 reprint the DOI rides on), and
the report notes that a chapter/component DOI was cited.

**Google Books** supplements this where Open Library falls short. Open Library's title search is
strict — a title carrying its subtitle (*"Why Nations Fail: The Origins of Power, Prosperity, and
Poverty"*) or a single off-by-one ISBN can return nothing, so a real book is reported "not found
there". Google Books is more forgiving (`intitle:`/`inauthor:`/`isbn:`), and when the `.bib` carries
a Google Books **volume id** in its URL, that volume is resolved directly and pinned as authoritative
identity — exactly like a cited OpenAlex Work id. This matters because a same-titled journal-article
(e.g. a book *review* that reuses the book's title and authors and carries a DOI the book lacks) would
otherwise be matched and have its DOI wrongly backfilled onto the `@book`. For a `@book` or
`@incollection`, an edition Open Library confirms still takes precedence over either pin.

Results are cached in a local SQLite DB, so re-running on the same `.bib` makes **no** repeat
network or LLM calls for what succeeded. Errors are never cached: an entry left without a verdict,
a failed source query or an undetermined doi.org check is retried on the next run. A transient
outage never counts as "no match".
