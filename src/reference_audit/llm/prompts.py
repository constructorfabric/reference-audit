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
