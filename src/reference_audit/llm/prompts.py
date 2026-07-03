"""Prompt construction for the LLM adjudication calls.

Both prompts use AFFIRMATIVE polarity: the model must positively conclude sameness; absence of
evidence ⇒ "no". This is what makes a garbled entry's nearest fuzzy neighbour fail to match (T4).
"""

from __future__ import annotations

from reference_audit.models import BibEntry, FeatureVector, SourceRecord

CAN_CORRESPOND_SYSTEM = (
    "You verify whether a returned database record is the SAME PUBLISHED ARTIFACT that a "
    "bibliography entry refers to. Titles and author names may be spelled differently, "
    "transliterated, abbreviated, or have a missing field (e.g. a dropped co-author or wrong "
    "month) — do NOT reject for those. Set can_correspond=true ONLY if you can affirmatively "
    "conclude they are the same document. Set can_correspond=false when there is positive evidence "
    "they are DIFFERENT works: a different scope or topic in the title tail, disjoint page ranges "
    "in the same venue, an author set that differs by more than spelling, or a clearly different "
    "subject. A merely missing field is not evidence of difference. Respond in strict JSON."
)

SAME_WORK_SYSTEM = (
    "Decide whether two database records describe the SAME WORK. Choose exactly one relation: "
    "'same_artifact' (the identical item), 'versions_of_same_work' (a preprint and its published "
    "version, or different editions/printings of one work), 'distinct_works' (different papers, "
    "even if by the same authors in the same venue/year), or 'uncertain'. Two papers with "
    "different titles and non-overlapping page ranges are distinct_works. Conclude 'same' or "
    "'versions' ONLY with positive evidence. Respond in strict JSON."
)

WEB_MATCH_SYSTEM = (
    "A bibliography entry cites a WEB PAGE by URL — a blog post, a software/project page, "
    "documentation, a dataset, or another non-journal resource. You are given the .bib entry and the "
    "title, metadata, and text of the page actually fetched from that URL. Decide whether the fetched "
    "page IS the resource the entry cites. Set corresponds=true ONLY if you can affirmatively conclude "
    "the page is that resource — its title/topic/authors match the citation, allowing for spelling, "
    "abbreviation, transliteration, or an appended site name. Set corresponds=false ONLY with positive "
    "evidence the page is something else: a different article, a site homepage or index/listing, a "
    "login/paywall wall, a 'page not found'/error notice, or an unrelated topic. When the page has too "
    "little content to tell, or it is ambiguous, use low confidence rather than guessing. Respond in "
    "strict JSON."
)

FIELD_CHECK_SYSTEM = (
    "A bibliography entry has already been matched, by identifier, to a specific real publication, "
    "so the two refer to the SAME work. You judge ONE field of that entry against the authoritative "
    "database value and decide whether the entry's value is merely formatted differently or is a "
    "genuine mistake.\n"
    "Classify 'formatting_variant' (NOT a mistake) for: capitalization; punctuation; accents and "
    "transliteration (Müller/Mueller); LaTeX braces; standard abbreviations vs full forms of a "
    "journal or conference (e.g. 'J. Theor. Biol.' = 'Journal of Theoretical Biology', 'NeurIPS' = "
    "'Advances in Neural Information Processing Systems'); an added or dropped 'Proceedings of'/year/"
    "edition qualifier on a conference series; en-dash vs hyphen; 'and' vs '&'; word-order in a "
    "series name.\n"
    "Classify 'error' for a substantively different value: a different number; a wrong, misspelled, "
    "or split word (e.g. 'Un iversity'); a missing word that changes the name (e.g. a journal "
    "written 'Annual Review Condensed Matter Physics', dropping 'of'); a truncated or different "
    "title or venue; a value that belongs to a different field.\n"
    "Databases are themselves sometimes wrong or incomplete, and the bibliography value is usually "
    "correct: \n"
    "- If the database VENUE is a preprint server, institutional repository, or aggregator "
    "('arXiv', 'bioRxiv', 'medRxiv', 'SSRN', 'ResearchGate', 'Radboud Repository', 'HAL', a "
    "university or 'Technical Reports Server') while the entry names a journal or conference, the "
    "database simply indexed a different copy — classify 'formatting_variant', not 'error'.\n"
    "- If the database value looks like a TRUNCATION or substring of the entry's value (e.g. "
    "database 'Complex' vs entry 'Complexity'), the entry is the fuller, correct form — classify "
    "'formatting_variant' or 'uncertain', never 'error'.\n"
    "If the entry's value is plausibly correct and the database merely differs, prefer 'uncertain' "
    "over 'error'. Use 'uncertain' whenever you cannot affirmatively decide. Respond in strict JSON."
)


CITATION_ALIGNMENT_SYSTEM = (
    "A bibliography entry has been matched to a specific real publication (the CITED WORK). You are "
    "given (1) the sentence(s) from the citing paper that invoke the cited work — the CLAIM the "
    "author attaches to the citation — and (2) the ABSTRACT of the cited work. Decide whether the "
    "cited work's abstract is consistent with the way it is cited.\n"
    "Classify 'supported' when the abstract affirmatively corroborates the citing claim.\n"
    "Classify 'contradicted' ONLY with positive evidence that the abstract asserts the OPPOSITE of "
    "the citing claim (the claim attributes to the work a finding the abstract explicitly refutes, "
    "reverses, or excludes).\n"
    "Classify 'not_in_abstract' when the abstract neither supports nor contradicts the claim — it is "
    "silent on that point. An abstract is only a summary, so its silence is NOT evidence of misuse: "
    "prefer 'not_in_abstract' over 'contradicted' whenever the abstract simply does not address the "
    "claim. Do NOT use outside knowledge; judge only against the abstract text provided.\n"
    "For a 'supported' or 'contradicted' verdict, put the short span of the abstract that justifies "
    "it in evidence_quote (leave it empty for 'not_in_abstract'). Respond in strict JSON."
)

# Cap the abstract text fed to the model: enough to judge the claim, bounded for cost.
ABSTRACT_TEXT_LIMIT = 4000


def citation_alignment_user(entry: BibEntry, context_text: str, abstract: str) -> str:
    return (
        "CITED WORK:\n"
        f"  title:   {entry.title or '(none)'}\n"
        f"  authors: {'; '.join(entry.authors) or '(none)'}\n"
        f"  year:    {entry.year or '(none)'}\n\n"
        "HOW IT IS CITED (the claim the citing paper attaches to this reference):\n"
        f"  {context_text}\n\n"
        "ABSTRACT OF THE CITED WORK:\n"
        f"{abstract[:ABSTRACT_TEXT_LIMIT] or '(none)'}\n\n"
        "Is the citing claim supported by, contradicted by, or simply not addressed in the abstract?"
    )


def field_check_user(
    field: str, bib_value: str, canonical_value: str, sources: list[str], entry: BibEntry
) -> str:
    src = ", ".join(sources) if sources else "database"
    return (
        f"FIELD UNDER REVIEW: {field}\n"
        f"  citation (.bib) value:  {bib_value or '(empty)'}\n"
        f"  authoritative value:    {canonical_value or '(empty)'}   [source: {src}]\n\n"
        "CONTEXT — the same work, confirmed by identifier (for grounding only):\n"
        f"  title:   {entry.title}\n"
        f"  authors: {'; '.join(entry.authors) or '(none)'}\n"
        f"  year:    {entry.year or '(none)'}\n"
        f"  type:    {entry.entry_type.value}\n\n"
        f"Is the .bib {field} value the same as the authoritative value apart from formatting, "
        "or is it a genuine mistake?"
    )


def _fmt_entry(entry: BibEntry) -> str:
    ids = entry.ids
    return (
        f"  title:   {entry.title}\n"
        f"  authors: {'; '.join(entry.authors) or '(none)'}\n"
        f"  year:    {entry.year or '(none)'}\n"
        f"  venue:   {entry.venue or '(none)'}\n"
        f"  pages:   {entry.pages or '(none)'}\n"
        f"  ids:     doi={ids.doi} arxiv={ids.arxiv_id} isbn={ids.isbn13}"
    )


def _fmt_record(rec: SourceRecord) -> str:
    ids = rec.ids
    return (
        f"  source:  {rec.source}\n"
        f"  title:   {rec.title}\n"
        f"  authors: {'; '.join(rec.authors) or '(none)'}\n"
        f"  year:    {rec.year or '(none)'}\n"
        f"  venue:   {rec.venue or '(none)'}\n"
        f"  pages:   {rec.pages or '(none)'}\n"
        f"  ids:     doi={ids.doi} arxiv={ids.arxiv_id} isbn={ids.isbn13}"
    )


def _fmt_flags(f: FeatureVector) -> str:
    return (
        f"  title_prefix_trap={f.title_prefix_trap} pages_conflict={f.pages_conflict} "
        f"author_set_jaccard={f.author_set_jaccard:.2f} id_agreement={f.id_agreement} "
        f"title_ratio={f.title_ratio:.2f}"
    )


def can_correspond_user(entry: BibEntry, rec: SourceRecord, features: FeatureVector) -> str:
    return (
        "BIB ENTRY:\n" + _fmt_entry(entry) + "\n\n"
        "DATABASE RECORD:\n" + _fmt_record(rec) + "\n\n"
        "ATTENTION FLAGS (formal signals; weigh them):\n" + _fmt_flags(features) + "\n\n"
        "Does the database record correspond to the same published artifact as the bib entry?"
    )


def same_work_user(a: SourceRecord, b: SourceRecord) -> str:
    return (
        "RECORD A:\n" + _fmt_record(a) + "\n\n"
        "RECORD B:\n" + _fmt_record(b) + "\n\n"
        "What is the relation between record A and record B?"
    )


# Cap the fetched page text fed to the model: enough to judge identity, bounded for cost.
WEB_PAGE_TEXT_LIMIT = 4000


def web_match_user(entry: BibEntry, page: SourceRecord) -> str:
    raw = page.raw or {}
    text = (raw.get("text") or "").strip()[:WEB_PAGE_TEXT_LIMIT]
    return (
        "CITED REFERENCE (.bib):\n"
        f"  title:     {entry.title or '(none)'}\n"
        f"  authors:   {'; '.join(entry.authors) or '(none)'}\n"
        f"  year:      {entry.year or '(none)'}\n"
        f"  cited url: {entry.ids.url or '(none)'}\n\n"
        "FETCHED PAGE (from the cited URL):\n"
        f"  final url:    {page.ids.url or '(none)'}\n"
        f"  meta title:   {page.title or '(none)'}\n"
        f"  meta authors: {'; '.join(page.authors) or '(none)'}\n"
        f"  site name:    {raw.get('site_name') or '(none)'}\n"
        f"  description:  {raw.get('description') or '(none)'}\n\n"
        "PAGE TEXT (truncated):\n"
        f"{text or '(no extractable text)'}\n\n"
        "Is the fetched page the same resource the .bib entry cites?"
    )
