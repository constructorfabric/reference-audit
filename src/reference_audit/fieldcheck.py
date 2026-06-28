"""Step 3 (correctness): are the .bib *fields* right?

Once step 1 has matched an entry to exactly one real artifact (by a strong identifier), the work's
identity is settled — but individual fields can still be wrong: a dropped word in a journal name, a
split typo ('Un iversity'), a placeholder issue number, a garbled page range. This module compares
each field of the entry against the canonical record and labels it `ok` / `formatting` / `error` /
`uncertain` / `unverifiable`.

The classifier is an intelligent combination of two layers:

1. **Per-field deterministic rules** (`deterministic_field_checks`, pure, no network). These own the
   cases a rule can settle: numeric fields (year/volume/issue) compare by value; page ranges are
   normalized so that dash style (`--` vs en-dash vs the malformed `185-–197`) never reads as a
   wrong page; empty/placeholder fields are flagged outright; exact and case/accent-folded string
   matches resolve to `ok`/`formatting`.
2. **An LLM check** for the genuinely ambiguous string differences a rule cannot judge — is
   'Annual Review Condensed Matter Physics' a wrong name (dropped 'of') or just a variant? is
   'J. Theor. Biol.' the same journal as 'Journal of Theoretical Biology'? `resolve_field_findings`
   escalates exactly those, caching each decision.

Per the reliability contract, an unconfirmable field becomes `unverifiable`/`uncertain` and is
reported — never silently passed. Field checks are advisory: they never change the step-1 verdict.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field as dataclass_field

from anyascii import anyascii

from reference_audit.cache.store import AuditCache, prompt_hash
from reference_audit.config import AuditConfig
from reference_audit.llm.client import LLMClient, LLMError
from reference_audit.llm.prompts import FIELD_CHECK_SYSTEM, field_check_user
from reference_audit.models import (
    BibEntry,
    EntryType,
    FieldFinding,
    FieldJudgment,
    MatchedArtifact,
    SourceRecord,
)
from reference_audit.parsing.identifiers import arxiv_submission_year
from reference_audit.versioning import cited_arxiv_id

_BOOK_TYPES = {EntryType.BOOK, EntryType.INCOLLECTION}

# A "venue" that is really a preprint server, institutional repository, or aggregator. When the
# matched record's venue looks like one of these, the database indexed a preprint/repository copy,
# so the entry's published journal/conference cannot be confirmed against it — and a difference is
# NOT a bib mistake. (Deterministic guard; the LLM is unreliable at applying this on its own.)
_REPOSITORY_VENUE_RE = re.compile(
    r"arxiv|bio\s*rxiv|med\s*rxiv|chem\s*rxiv|preprint|repositor|researchgate|\bssrn\b|zenodo|"
    r"figshare|\bosf\b|hal[-\s]|scholarworks|dspace|eprints|research\s+square|"
    r"technical reports server|\bscholar \(|\(.*\buniversit",
    re.IGNORECASE,
)

# Values that mean "no real value" even though the field is present (sotnikov `number={}`,
# goldenfeld `number={-}`). Compared after stripping braces/whitespace and lowercasing.
_PLACEHOLDERS = {"", "-", "--", "–", "—", "n/a", "n.a.", "na", "none", "null", "tbd", "?", "..."}

# Source preference for sourcing a canonical field value: the publisher of record first, then the
# rich registration-grade bibliographic records.
_SOURCE_RANK = {
    "publisher": 0,
    "crossref": 1,
    "openalex": 2,
    "openlibrary": 3,
    "semantic_scholar": 4,
    "arxiv": 5,
}

_DASH_RUN = re.compile(r"\s*[-–—‐]+\s*")
_RANGE = re.compile(r"\w-\w")


def _strip(s: str | None) -> str:
    """Collapse whitespace and drop LaTeX braces, preserving case/accents."""
    return re.sub(r"\s+", " ", (s or "").replace("{", "").replace("}", "")).strip()


def _fold(s: str | None) -> str:
    """Case/accent/punctuation-insensitive form for equivalence. Braces are *removed* (so the
    protection in '{L}enia' rejoins 'Lenia') before other punctuation collapses to spaces."""
    bare = (s or "").replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", anyascii(bare).lower())).strip()


def _is_placeholder(raw: str) -> bool:
    return _strip(raw).lower() in _PLACEHOLDERS


def _num(s: str) -> str:
    """Normalize an integer-valued field (drop leading zeros); fall back to a folded string."""
    t = _strip(s)
    return str(int(t)) if t.isdigit() else _fold(t)


def _norm_pages(p: str) -> str:
    """Canonical page form for comparison: any dash run → single '-', folded ('E8678--E8687'→
    'e8678-e8687'; '139–-158'→'139-158'; '40'→'40')."""
    return _DASH_RUN.sub("-", _strip(p)).casefold()


def _pages_clean_range(raw: str) -> bool:
    """True if a page *range* is written with the canonical BibTeX '--' and nothing odd."""
    s = _strip(raw)
    if "–" in s or "—" in s:  # contains an en/em dash → not the canonical '--'
        return False
    return bool(re.search(r"\w--\w", s))


# ── canonical value sourcing ─────────────────────────────────────────────────


def _ordered_records(artifact: MatchedArtifact) -> list[SourceRecord]:
    """Records of the matched artifact, richest metadata first (published, crossref/openalex)."""
    return sorted(
        artifact.records,
        key=lambda r: (r.is_preprint, _SOURCE_RANK.get(r.source, 9)),
    )


def _rec_sources(rec: SourceRecord) -> list[str]:
    """Sources behind a (possibly pooled) record — the merge set if present, else its own source."""
    merged_from = rec.raw.get("merged_from") if isinstance(rec.raw, dict) else None
    return list(merged_from) if merged_from else [rec.source]


def _canonical(records: list[SourceRecord], getter) -> tuple[str, list[str]]:
    """First non-empty value for a field across the records, plus every source that agrees on it."""
    value = ""
    carrier = None
    for r in records:
        v = (getter(r) or "").strip()
        if v:
            value, carrier = v, r
            break
    if not value or carrier is None:
        return "", []
    return value, _rec_sources(carrier)


# ── per-field deterministic comparison ───────────────────────────────────────


@dataclass
class _Check:
    """A field comparison before (optional) LLM escalation. `needs_llm` marks the ambiguous
    string differences a rule cannot settle on its own."""

    field: str
    bib_value: str
    canonical_value: str
    sources: list[str] = dataclass_field(default_factory=list)
    status: str = "ok"  # ok | formatting | error | unverifiable | needs_llm
    detail: str = ""
    needs_llm: bool = False

    def finding(self, *, via_llm: bool = False) -> FieldFinding:
        return FieldFinding(
            field=self.field,
            bib_value=self.bib_value,
            canonical_value=self.canonical_value,
            sources=self.sources,
            status="uncertain" if self.status == "needs_llm" else self.status,
            detail=self.detail,
            via_llm=via_llm,
        )


def _string_field(name: str, bib_raw: str, canonical: str, sources: list[str]) -> _Check:
    """Exact → ok; case/accent-fold equal → formatting; otherwise escalate to the LLM."""
    bib = _strip(bib_raw)
    chk = _Check(name, bib, canonical, sources)
    if not canonical:
        chk.status, chk.detail = "unverifiable", "no source returned this field to check against"
        return chk
    if bib == _strip(canonical):
        return chk
    if _fold(bib) == _fold(canonical):
        chk.status = "formatting"
        chk.detail = f"only capitalization/punctuation/accents differ from canonical '{canonical}'"
        return chk
    chk.status, chk.needs_llm = "needs_llm", True
    return chk


def _venue_check(entry: BibEntry, records: list[SourceRecord]) -> _Check | None:
    if not entry.venue:
        return None
    canonical, sources = _canonical(records, lambda r: r.venue)
    if canonical and _REPOSITORY_VENUE_RE.search(canonical):
        chk = _Check("journal/venue", _strip(entry.venue), canonical, sources)
        chk.status = "unverifiable"
        chk.detail = (
            f"matched a preprint/repository copy ('{canonical}'); the published venue could "
            "not be confirmed"
        )
        return chk
    return _string_field("journal/venue", entry.venue, canonical, sources)


def _title_check(entry: BibEntry, records: list[SourceRecord]) -> _Check | None:
    if not entry.title:
        return None
    canonical, sources = _canonical(records, lambda r: r.title)
    chk = _Check("title", _strip(entry.title), canonical, sources)
    if not canonical:
        chk.status, chk.detail = "unverifiable", "no source returned a title to check against"
        return chk
    if _fold(entry.title) == _fold(canonical):
        return chk  # braces/case/accents folded away → ok
    chk.status, chk.needs_llm = "needs_llm", True
    return chk


def _year_check(entry: BibEntry, records: list[SourceRecord]) -> _Check | None:
    if entry.year is None:
        return None
    carrier = next((r for r in records if r.year), None)
    canonical_year = carrier.year if carrier else None
    sources = _rec_sources(carrier) if carrier else []
    chk = _Check("year", str(entry.year), str(canonical_year or ""), sources)
    if canonical_year is None:
        chk.status, chk.detail = "unverifiable", "no source returned a year to check against"
        return chk
    delta = abs(entry.year - canonical_year)
    if delta == 0:
        return chk
    # Preprint cited by its arXiv id: the .bib year is the original version's submission year
    # (encoded in the id). arXiv updates push the canonical/latest year forward, so a *later*
    # canonical year is a newer VERSION of the same preprint, not a wrong year — the record is valid
    # for the version cited (the newer-version advisory is emitted by versioning.better_version_notes).
    # A cited year that does NOT equal the id-encoded submission year falls through and is still flagged.
    arxiv_id = cited_arxiv_id(entry)
    if arxiv_id is not None and canonical_year > entry.year:
        submitted = arxiv_submission_year(arxiv_id)
        if submitted is not None and entry.year == submitted:
            chk.detail = (
                f"matches the cited arXiv version ({submitted}); canonical {canonical_year} is a "
                "later version of the same preprint"
            )
            return chk  # status stays 'ok' — valid for the version cited
    if entry.entry_type in _BOOK_TYPES:
        # Books have edition-specific years; the match may be a different printing, so a year gap
        # is a prompt to verify the edition — not, on its own, a bib mistake.
        chk.status = "uncertain"
        chk.detail = f"differs from matched edition ({canonical_year}); verify the edition/printing"
    elif delta == 1:
        chk.status = "uncertain"
        chk.detail = f"differs by one from canonical {canonical_year} (often online vs print year)"
    else:
        chk.status = "error"
        chk.detail = f"year {entry.year} does not match canonical {canonical_year}"
    return chk


def _numeric_field(
    name: str, bib_raw: str | None, canonical: str, sources: list[str]
) -> _Check | None:
    """volume / issue: present-but-placeholder → error; numeric/string compare otherwise."""
    if bib_raw is None:
        return None  # field absent from the entry — nothing to check
    chk = _Check(name, _strip(bib_raw), canonical, sources)
    if _is_placeholder(bib_raw):
        chk.status = "error"
        chk.detail = f"{name} is empty or a placeholder ('{_strip(bib_raw)}')"
        if canonical:
            chk.detail += f"; canonical {name} is '{canonical}'"
        return chk
    if not canonical:
        chk.status, chk.detail = "unverifiable", f"no source returned {name} to check against"
        return chk
    if _num(bib_raw) != _num(canonical):
        chk.status = "error"
        chk.detail = f"{name} '{_strip(bib_raw)}' does not match canonical '{canonical}'"
    return chk


def _pages_check(entry: BibEntry, records: list[SourceRecord]) -> _Check | None:
    if not entry.pages:
        return None
    canonical, sources = _canonical(records, lambda r: r.pages)
    chk = _Check("pages", _strip(entry.pages), canonical, sources)
    if not canonical:
        chk.status, chk.detail = "unverifiable", "no source returned pages to check against"
        return chk
    if _norm_pages(entry.pages) != _norm_pages(canonical):
        chk.status = "error"
        cano, ent = _norm_pages(canonical), _norm_pages(entry.pages)
        if "-" not in cano and "-" in ent and ent.split("-", 1)[0] == cano:
            # canonical is a single article number (e.g. an article-numbered proceedings); the entry
            # turned it into a range, inventing an end page that no source confirms.
            chk.detail = (
                f"canonical lists a single article number '{canonical}'; the entry's range "
                f"'{_strip(entry.pages)}' adds an end page no source confirms"
            )
        else:
            chk.detail = f"pages '{_strip(entry.pages)}' do not match canonical '{canonical}'"
        return chk
    # Same page numbers: only the separator style may differ.
    if _RANGE.search(_norm_pages(entry.pages)) and not _pages_clean_range(entry.pages):
        chk.status = "formatting"
        chk.detail = f"non-standard page separator in '{_strip(entry.pages)}'; use '--'"
    return chk


def deterministic_field_checks(
    entry: BibEntry,
    artifact: MatchedArtifact,
    *,
    skip_fields: frozenset[str] = frozenset(),
) -> list[_Check]:
    """All per-field rule outcomes for a matched entry (pure, no network/LLM).

    `skip_fields` drops named fields — used for books, whose `year`/`publisher` are edition-specific
    and verified separately against the cited Open Library edition (see `check_book_edition_fields`),
    not against the pooled artifact (which may hold a newer edition).
    """
    records = _ordered_records(artifact)
    checks: list[_Check | None] = [
        _title_check(entry, records),
        _venue_check(entry, records),
        _year_check(entry, records),
    ]

    vol_canon, vol_src = _canonical(records, lambda r: r.volume)
    checks.append(_numeric_field("volume", entry.raw_fields.get("volume"), vol_canon, vol_src))

    # The entry may carry the issue under the standard `number` or the variant `issue` field.
    number_raw = entry.raw_fields.get("number")
    if number_raw is None:
        number_raw = entry.raw_fields.get("issue")
    iss_canon, iss_src = _canonical(records, lambda r: r.issue)
    checks.append(_numeric_field("number", number_raw, iss_canon, iss_src))

    checks.append(_pages_check(entry, records))

    if entry.publisher:
        canonical, sources = _canonical(records, lambda r: r.publisher)
        checks.append(_string_field("publisher", entry.publisher, canonical, sources))

    return [c for c in checks if c is not None and c.field not in skip_fields]


# ── LLM escalation ───────────────────────────────────────────────────────────


def _apply_judgment(chk: _Check, judgment: FieldJudgment) -> FieldFinding:
    finding = chk.finding(via_llm=True)
    finding.detail = judgment.reason
    if judgment.classification == "formatting_variant":
        finding.status = "formatting"
    elif judgment.classification == "error" and judgment.confidence in ("high", "medium"):
        finding.status = "error"
    else:  # low-confidence error or explicit uncertain → don't over-claim
        finding.status = "uncertain"
    return finding


async def _judge_field(
    entry: BibEntry,
    chk: _Check,
    llm: LLMClient | None,
    cache: AuditCache | None,
) -> FieldFinding:
    if llm is None:
        finding = chk.finding()
        finding.status = "uncertain"
        finding.detail = (
            f"differs from canonical '{chk.canonical_value}'; LLM unavailable, verify manually"
        )
        return finding
    user = field_check_user(chk.field, chk.bib_value, chk.canonical_value, chk.sources, entry)
    p_hash = prompt_hash(FIELD_CHECK_SYSTEM + "\n" + user)
    if cache is not None:
        cached = cache.get_llm_decision(p_hash, "field_check")
        if cached is not None:
            return _apply_judgment(chk, FieldJudgment.model_validate_json(cached))
    try:
        judgment = await llm.structured(FIELD_CHECK_SYSTEM, user, FieldJudgment, "field_check")
    except LLMError as exc:
        finding = chk.finding()
        finding.status = "uncertain"
        finding.detail = f"differs from canonical '{chk.canonical_value}'; LLM check failed ({exc})"
        return finding
    if cache is not None:
        cache.put_llm_decision(p_hash, "field_check", judgment.model_dump_json())
    return _apply_judgment(chk, judgment)


async def resolve_field_findings(
    entry: BibEntry,
    artifact: MatchedArtifact,
    llm: LLMClient | None,
    config: AuditConfig,
    cache: AuditCache | None,
    *,
    skip_fields: frozenset[str] = frozenset(),
) -> list[FieldFinding]:
    """Full step-3 result for one matched entry: deterministic rules + LLM tie-break, in order."""
    checks = deterministic_field_checks(entry, artifact, skip_fields=skip_fields)
    escalated = await asyncio.gather(
        *(_judge_field(entry, c, llm, cache) for c in checks if c.needs_llm)
    )
    escalated_iter = iter(escalated)
    return [next(escalated_iter) if c.needs_llm else c.finding() for c in checks]


def _book_publisher_check(entry: BibEntry, canonical: str, sources: list[str]) -> _Check:
    """Publisher check against the cited edition's publisher.

    A folded-substring relationship ('W. A. Benjamin' ⊆ 'W. A. Benjamin, Advanced Book Program') is a
    shortened-but-correct imprint name → `formatting`, settled without the LLM. A genuinely different
    string (a typo like 'Princeton Un iversity Press', or a different publisher) still escalates so it
    is judged, not silently passed.
    """
    bib = _strip(entry.publisher)
    chk = _Check("publisher", bib, canonical, sources)
    if not canonical:
        chk.status, chk.detail = "unverifiable", "the matched Open Library edition lists no publisher"
        return chk
    if bib == _strip(canonical):
        return chk
    a, b = _fold(bib), _fold(canonical)
    if a and b and (a in b or b in a):
        chk.status = "formatting"
        chk.detail = f"a shortened form of the edition's publisher '{canonical}'"
        return chk
    chk.status, chk.needs_llm = "needs_llm", True
    return chk


async def check_book_edition_fields(
    entry: BibEntry,
    matched_edition: SourceRecord,
    llm: LLMClient | None,
    config: AuditConfig,  # noqa: ARG001 — symmetry with resolve_field_findings; reserved
    cache: AuditCache | None,
) -> list[FieldFinding]:
    """Verify a book's `year`/`publisher` against the *cited* Open Library edition.

    Grounding the canonical values in the matched edition (rather than the pooled artifact, which may
    carry a newer reprint) means the original edition is no longer flagged against a later one. A
    genuine publisher typo (e.g. 'Princeton Un iversity Press') still escalates to the LLM and is
    caught.
    """
    records = [matched_edition]
    checks: list[_Check] = []
    year_chk = _year_check(entry, records)
    if year_chk is not None:
        checks.append(year_chk)
    if entry.publisher:
        canonical, sources = _canonical(records, lambda r: r.publisher)
        checks.append(_book_publisher_check(entry, canonical, sources))
    return [
        await _judge_field(entry, c, llm, cache) if c.needs_llm else c.finding()
        for c in checks
    ]


def consulted_sources(artifact: MatchedArtifact) -> list[str]:
    """Every source actually behind the matched artifact's records — i.e. exactly what the field
    checks compared against. Used to report an unverifiable field as "not present in {these
    sources}", never as the unprovable claim that no authoritative source has it."""
    return sorted({s for r in artifact.records for s in _rec_sources(r)})


def finding_note(f: FieldFinding) -> str:
    """One-line human summary for the text report (only actionable findings are surfaced)."""
    src = f" [{', '.join(f.sources)}]" if f.sources else ""
    tag = "(LLM) " if f.via_llm else ""
    if f.status == "error":
        return (
            f"{tag}field '{f.field}' looks wrong: '{f.bib_value}' vs canonical "
            f"'{f.canonical_value}'{src} — {f.detail}"
        )
    if f.status == "uncertain":
        return (
            f"{tag}field '{f.field}' needs review: '{f.bib_value}' vs canonical "
            f"'{f.canonical_value}'{src} — {f.detail}"
        )
    return f"field '{f.field}'='{f.bib_value}' could not be verified — {f.detail}"
