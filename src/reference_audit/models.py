"""All pydantic data models — the single source of truth.

Seeded from `paper-search-mcp` `Paper` and `sciwrite-lint` `Citation`, promoted to pydantic
with an explicit `Identifiers` value object and a version graph (`version_links`,
`openalex_work_id`) that lets the matcher recover preprint↔published relations (the T1 fix).
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class EntryType(StrEnum):
    ARTICLE = "article"
    INPROCEEDINGS = "inproceedings"
    BOOK = "book"
    INCOLLECTION = "incollection"
    MISC = "misc"
    UNKNOWN = "unknown"


# bib type string -> EntryType
_BIB_TYPE_MAP = {
    "article": EntryType.ARTICLE,
    "inproceedings": EntryType.INPROCEEDINGS,
    "conference": EntryType.INPROCEEDINGS,
    "book": EntryType.BOOK,
    "inbook": EntryType.INCOLLECTION,
    "incollection": EntryType.INCOLLECTION,
    "misc": EntryType.MISC,
    "online": EntryType.MISC,
    "electronic": EntryType.MISC,
    "unpublished": EntryType.MISC,
    "techreport": EntryType.MISC,
}


def entry_type_from_bib(bib_type: str) -> EntryType:
    return _BIB_TYPE_MAP.get((bib_type or "").strip().lower(), EntryType.UNKNOWN)


def _norm_token_string(s: str) -> str:
    """Lowercase, collapse non-alphanumerics to single spaces — for stable hashing."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


class Identifiers(BaseModel):
    """Normalized identifiers. DOIs are stored bare + lowercased (opaque-token equality)."""

    doi: str | None = None          # bare, e.g. "10.1073/pnas.2004976117" (no https://doi.org/)
    arxiv_id: str | None = None     # e.g. "2412.17799"
    isbn13: str | None = None       # 13-digit, separators stripped
    pmid: str | None = None
    bibcode: str | None = None      # NASA ADS
    openalex: str | None = None     # OpenAlex Work id, bare + upper, e.g. "W3034344071"
    url: str | None = None

    def primary_kind(self) -> Literal["doi", "isbn", "arxiv", "openalex", "url"] | None:
        if self.doi:
            return "doi"
        if self.isbn13:
            return "isbn"
        if self.arxiv_id:
            return "arxiv"
        if self.openalex:
            return "openalex"
        if self.url:
            return "url"
        return None

    def any_present(self) -> bool:
        return any(
            (self.doi, self.arxiv_id, self.isbn13, self.pmid, self.bibcode, self.openalex, self.url)
        )


def compute_content_hash(
    title: str, authors: list[str], year: int | None, venue: str, ids: Identifiers
) -> str:
    """sha256 over normalized identifying fields. Cosmetic edits don't bust the cache;
    a real metadata change does."""
    parts = [
        _norm_token_string(title),
        "|".join(sorted(_norm_token_string(a) for a in authors)),
        str(year or ""),
        _norm_token_string(venue),
        ids.doi or "",
        ids.arxiv_id or "",
        ids.isbn13 or "",
        ids.openalex or "",
    ]
    return hashlib.sha256("␟".join(parts).encode("utf-8")).hexdigest()


class BibEntry(BaseModel):
    """One parsed .bib entry, with derived/normalized identifiers."""

    key: str
    entry_type: EntryType = EntryType.UNKNOWN
    title: str = ""
    authors: list[str] = Field(default_factory=list)   # surface strings, as written
    year: int | None = None
    venue: str = ""                                     # journal OR booktitle
    publisher: str = ""                                 # NOT a match feature (gavrilets typo)
    pages: str = ""
    ids: Identifiers = Field(default_factory=Identifiers)
    raw_fields: dict[str, str] = Field(default_factory=dict)
    cited: bool = False                                 # set by parsing/tex
    is_commented: bool = False                          # the bagrov2024visual twin; informational
    content_hash: str = ""

    @model_validator(mode="after")
    def _fill_hash(self) -> BibEntry:
        if not self.content_hash:
            self.content_hash = compute_content_hash(
                self.title, self.authors, self.year, self.venue, self.ids
            )
        return self


class SourceRecord(BaseModel):
    """One candidate, normalized across sources."""

    source: str
    source_native_id: str = ""        # OpenAlex Wxxx / S2 id / crossref DOI — dedupe within source
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str = ""
    volume: str = ""
    issue: str = ""                   # the canonical issue/number
    pages: str = ""
    publisher: str = ""
    ids: Identifiers = Field(default_factory=Identifiers)
    is_preprint: bool = False
    edition: int | None = None
    citation_count: int = 0
    version_links: list[str] = Field(default_factory=list)  # locations + relation targets (version graph)
    openalex_work_id: str | None = None                     # merge anchor (M4)
    raw: dict = Field(default_factory=dict)


class SourceQueryResult(BaseModel):
    """Result of one adapter call. `error` is distinct from an empty record list:
    an outage must never be cached/treated as 'not found'."""

    source: str
    query_kind: Literal["id", "metadata", "editions", "web"]
    records: list[SourceRecord] = Field(default_factory=list)
    error: str | None = None


class FeatureVector(BaseModel):
    """Interpretable features for one (entry, candidate) pair."""

    title_ratio: float = 0.0
    title_prefix_trap: bool = False
    author_overlap: float = 0.0
    author_set_jaccard: float = 0.0
    author_subset: bool = False           # one author set ⊆ other (abbreviation, not distinct work)
    year_factor: float = 1.0
    venue_compatible: float = 1.0
    id_agreement: Literal["match", "conflict", "absent"] = "absent"
    pages_conflict: bool = False
    composite: float = 0.0


class CanCorrespondResult(BaseModel):
    """LLM per-candidate filter verdict (strict json_schema)."""

    can_correspond: bool
    confidence: Literal["high", "medium", "low"]
    reason: str
    distinguishing_evidence: str = ""


class SameWorkResult(BaseModel):
    """LLM equivalence verdict for two records (strict json_schema)."""

    relation: Literal[
        "same_artifact", "versions_of_same_work", "distinct_works", "uncertain"
    ]
    confidence: Literal["high", "medium", "low"]
    reason: str


class WebMatchResult(BaseModel):
    """LLM verdict on whether a fetched web page IS the resource a URL-only @misc entry cites
    (strict json_schema). Affirmative polarity: ``corresponds`` is true only on positive evidence
    the page is that resource, false only on positive evidence it is something else (a different
    article, a homepage/index, a login/paywall/404 notice); otherwise low confidence.
    """

    corresponds: bool
    confidence: Literal["high", "medium", "low"]
    reason: str


class FieldJudgment(BaseModel):
    """LLM classification of one ambiguous field discrepancy (strict json_schema).

    `formatting_variant` = the .bib value denotes the same thing as the database value, differing
    only in style (abbreviation, capitalization, accents, punctuation, braces). `error` = the .bib
    value is genuinely wrong (a different/garbled/truncated value). `uncertain` when neither can be
    affirmatively concluded — the entry is then surfaced for manual review, never silently passed.
    """

    classification: Literal["formatting_variant", "error", "uncertain"]
    confidence: Literal["high", "medium", "low"]
    reason: str


class FieldFinding(BaseModel):
    """Step-3 correctness check of one .bib field against the identified canonical record.

    Runs only on an `exactly_one` match (the artifact is confirmed to be the cited work), so a
    difference is a property of the *field*, not evidence about identity. `status`:
      - ``ok``           value agrees with the canonical record (after normalization).
      - ``formatting``   differs only in formatting/style — not a mistake, a cosmetic fix at most.
      - ``error``        a genuine mistake (wrong, garbled, or empty/placeholder value).
      - ``uncertain``    could not be classified confidently — surfaced for manual review.
      - ``unverifiable`` the .bib has a value but no source returned this field to check it against.
    """

    field: str
    bib_value: str = ""
    canonical_value: str = ""
    sources: list[str] = Field(default_factory=list)  # source(s) carrying the canonical value
    status: Literal["ok", "formatting", "error", "uncertain", "unverifiable"]
    detail: str = ""
    via_llm: bool = False


class CandidateAssessment(BaseModel):
    record: SourceRecord
    features: FeatureVector
    bucket: Literal["auto_accept", "auto_reject", "adjudicate"]
    llm: CanCorrespondResult | None = None


class MatchedArtifact(BaseModel):
    """One real-world object an entry matched (possibly several merged source records)."""

    records: list[SourceRecord] = Field(default_factory=list)
    merged_ids: Identifiers = Field(default_factory=Identifiers)
    versions: list[SourceRecord] = Field(default_factory=list)  # preprint + published / editions
    best_record: SourceRecord | None = None


class Verdict(BaseModel):
    """README 5.1 / 5.2 / 5.3."""

    kind: Literal["none", "exactly_one", "multiple"]
    artifacts: list[MatchedArtifact] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "low"
    rationale: str = ""


class EntryAudit(BaseModel):
    entry: BibEntry
    candidates: list[CandidateAssessment] = Field(default_factory=list)
    verdict: Verdict | None = None          # None until step-1 matching has run
    field_findings: list[FieldFinding] = Field(default_factory=list)  # step 3 field correctness
    canonical_bibtex: str = ""              # step 3 (follow-on)
    issues: list[str] = Field(default_factory=list)
    from_cache: bool = False


class AuditReport(BaseModel):
    entries: list[EntryAudit] = Field(default_factory=list)
    cited_but_missing: list[str] = Field(default_factory=list)  # in .tex, absent from .bib
    uncited: list[str] = Field(default_factory=list)            # in .bib, never cited
    commented_twins: list[str] = Field(default_factory=list)    # informational (T1 context)
    missing_includes: list[str] = Field(default_factory=list)   # \input targets not found on disk
    summary: dict = Field(default_factory=dict)
