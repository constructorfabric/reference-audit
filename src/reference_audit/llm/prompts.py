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
