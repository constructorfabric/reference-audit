r"""Compare GROBID-extracted references against the ``.bib`` they were compiled from.

The correspondence problem and its answer: a PDF has no BibTeX keys, so extracted references cannot be
joined to ``.bib`` entries by name. But the *compile* produces the join for us —

* ``<version>.aux`` holds a ``\bibcite{key}{label}`` line for exactly the entries BibTeX emitted, so
  it is the precise ground-truth key set (not "everything in the ``.bib``", and not
  ``parse_cited_keys``, which shifts with which ``\input`` files happen to be on disk);
* ``<version>.bbl`` lists ``\bibitem``\ s in printed order, which is also the order GROBID reports
  ``<biblStruct>``\ s in.

So position gives a *hypothesis* (extracted #i ↔ printed #i) and content matching verifies it. Keeping
both matters: positional disagreement is what reveals GROBID dropping or merging references, which a
plain recall count hides — two references fused into one still "matches" one of them.

Similarity uses ``matching/`` primitives directly on two ``BibEntry`` objects. No fake ``SourceRecord``
is constructed: we want the same notion of "same work" the auditor uses, not a re-implementation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from reference_audit.matching.features import id_agreement, title_ratio
from reference_audit.matching.names import author_overlap, author_subset
from reference_audit.models import BibEntry
from reference_audit.parsing.bib import parse_bib

_BIBCITE_RE = re.compile(r"\\bibcite\s*\{([^}]+)\}")
_BIBITEM_RE = re.compile(r"\\bibitem(?:\s*\[[^\]]*\])?\s*\{([^}]+)\}")

# Match tiers, loosest last. A tier is recorded per pair so a metrics table shows *how* each reference
# was recognized — 'title only' pairs are where extraction quality is actually degrading.
TIER_ID = "id"
TIER_TITLE_AUTHOR = "title+author"
TIER_TITLE = "title"

_TITLE_AUTHOR_FLOOR = 0.90
_AUTHOR_FLOOR = 0.50
_TITLE_ONLY_FLOOR = 0.95


def printed_order(aux_or_bbl: Path) -> list[str]:
    r"""Ground-truth keys in printed order, from a ``.bbl`` (``\bibitem``) or ``.aux`` (``\bibcite``)."""
    text = aux_or_bbl.read_text(encoding="utf-8", errors="replace")
    regex = _BIBITEM_RE if aux_or_bbl.suffix == ".bbl" else _BIBCITE_RE
    seen: list[str] = []
    for m in regex.finditer(text):
        key = m.group(1).strip()
        if key and key not in seen:
            seen.append(key)
    return seen


def ground_truth(bib_path: Path, bbl_path: Path) -> list[BibEntry]:
    """The `.bib` entries that were actually printed, in printed order.

    An entry named in the `.bbl` but absent from the `.bib` cannot happen for a document we compiled
    ourselves; if it ever does, it is a harness bug and must surface rather than be skipped silently.
    """
    entries, _, _ = parse_bib(bib_path)
    by_key = {e.key: e for e in entries}
    order = printed_order(bbl_path)
    missing = [k for k in order if k not in by_key]
    if missing:
        raise RuntimeError(
            f"{bbl_path.name} prints keys absent from {bib_path.name}: {missing}. "
            "The build and the fixture are out of sync."
        )
    return [by_key[k] for k in order]


@dataclass
class Pair:
    """One matched (ground-truth, extracted) reference pair."""

    truth: BibEntry
    got: BibEntry
    tier: str
    truth_index: int
    got_index: int

    @property
    def positional(self) -> bool:
        return self.truth_index == self.got_index

    # Missing and wrong are tracked separately throughout, because they have opposite severities. A
    # field GROBID did not extract is a coverage gap: the auditor still matches the reference on what
    # it does have, and the field check reports the field as unverified. A field extracted *wrongly*
    # is an assertion about the world that is false — the dangerous case.

    @property
    def year_missing(self) -> bool:
        return self.truth.year is not None and self.got.year is None

    @property
    def year_wrong(self) -> bool:
        return (
            self.truth.year is not None
            and self.got.year is not None
            and self.truth.year != self.got.year
        )

    @property
    def doi_ok(self) -> bool:
        return bool(self.truth.ids.doi) and self.truth.ids.doi == self.got.ids.doi

    @property
    def doi_wrong(self) -> bool:
        """A DOI was extracted and it is not the printed one — a fabricated identifier.

        Far worse than a missing DOI: it sends the auditor to a real but different document, and the
        report would then confidently describe the wrong paper.
        """
        return bool(self.got.ids.doi) and bool(self.truth.ids.doi) and not self.doi_ok

    @property
    def doi_unexpected(self) -> bool:
        """A DOI was extracted for a reference whose `.bib` entry has none.

        Also a fabrication, and it has to be counted separately because `doi_wrong` cannot see it —
        there is no printed DOI to disagree with. A bibliography style cannot print a DOI the entry
        does not have, so the value came from somewhere else in the page: in practice GROBID merging
        two adjacent references, or attaching an identifier it found nearby.
        """
        return bool(self.got.ids.doi) and not self.truth.ids.doi


@dataclass
class Comparison:
    pairs: list[Pair] = field(default_factory=list)
    unmatched_truth: list[BibEntry] = field(default_factory=list)
    spurious: list[BibEntry] = field(default_factory=list)
    n_truth: int = 0
    n_extracted: int = 0

    @property
    def matched(self) -> int:
        return len(self.pairs)

    @property
    def year_wrong(self) -> int:
        return sum(1 for p in self.pairs if p.year_wrong)

    @property
    def year_missing(self) -> int:
        return sum(1 for p in self.pairs if p.year_missing)

    @property
    def doi_extracted(self) -> int:
        return sum(1 for p in self.pairs if p.got.ids.doi)

    @property
    def doi_correct(self) -> int:
        return sum(1 for p in self.pairs if p.doi_ok)

    @property
    def doi_wrong(self) -> int:
        return sum(1 for p in self.pairs if p.doi_wrong)

    @property
    def doi_unexpected(self) -> int:
        return sum(1 for p in self.pairs if p.doi_unexpected)

    @property
    def doi_fabricated(self) -> int:
        """Every extracted DOI that is not the one printed, however it went wrong."""
        return self.doi_wrong + self.doi_unexpected

    @property
    def positional_agreement(self) -> int:
        return sum(1 for p in self.pairs if p.positional)

    def table(self, label: str) -> str:
        def pct(n: int, d: int) -> str:
            return f"{n}/{d}" + (f" ({n / d:.1%})" if d else "")

        tiers: dict[str, int] = {}
        for p in self.pairs:
            tiers[p.tier] = tiers.get(p.tier, 0) + 1
        return "\n".join(
            [
                f"--- {label} ---",
                f"  printed references     : {self.n_truth}",
                f"  extracted references   : {self.n_extracted}",
                f"  matched                : {pct(self.matched, self.n_truth)}"
                f"   [{', '.join(f'{k}={v}' for k, v in sorted(tiers.items()))}]",
                f"  unmatched printed      : {len(self.unmatched_truth)}",
                f"  spurious (matched none): {len(self.spurious)}",
                f"  positional agreement   : {pct(self.positional_agreement, self.matched)}",
                # DOI *precision*, not recall: a bibliography style that does not print DOIs (abbrv,
                # plain) gives GROBID nothing to read, so recall would measure the style, not GROBID.
                f"  DOI precision          : {pct(self.doi_correct, self.doi_extracted)}",
                f"  DOI wrong (disagrees)  : {self.doi_wrong}",
                f"  DOI unexpected (no gt) : {self.doi_unexpected}",
                f"  year wrong             : {self.year_wrong}",
                f"  year missing           : {self.year_missing}",
            ]
            + [f"      missed: {e.key} — {e.title[:70]}" for e in self.unmatched_truth[:10]]
        )


def _tier(truth: BibEntry, got: BibEntry) -> str | None:
    if id_agreement(truth.ids, got.ids) == "match":
        return TIER_ID
    ratio = title_ratio(truth.title, got.title)
    if ratio >= _TITLE_AUTHOR_FLOOR and (
        author_overlap(truth.authors, got.authors) >= _AUTHOR_FLOOR
        or author_subset(truth.authors, got.authors)
    ):
        return TIER_TITLE_AUTHOR
    if ratio >= _TITLE_ONLY_FLOOR:
        return TIER_TITLE
    return None


def _score(truth: BibEntry, got: BibEntry) -> float:
    ratio = title_ratio(truth.title, got.title)
    return ratio * (0.5 + 0.5 * author_overlap(truth.authors, got.authors))


def compare(truth: list[BibEntry], extracted: list[BibEntry]) -> Comparison:
    """Greedy one-to-one match of extracted references onto the printed ones.

    Identifier matches are consumed first (they are the strongest evidence and must not be lost to a
    high-scoring title collision), then the remaining candidates by descending similarity. One-to-one
    is the point: it makes "two printed references fused into one extraction" show up as an unmatched
    entry rather than being scored as a hit twice.
    """
    result = Comparison(n_truth=len(truth), n_extracted=len(extracted))
    candidates: list[tuple[float, int, int, str]] = []
    for i, t in enumerate(truth):
        for j, g in enumerate(extracted):
            tier = _tier(t, g)
            if tier is not None:
                candidates.append((_score(t, g), i, j, tier))

    # id-tier first, then by score; ties broken by index so the result is deterministic.
    candidates.sort(key=lambda c: (c[3] != TIER_ID, -c[0], c[1], c[2]))

    used_truth: set[int] = set()
    used_got: set[int] = set()
    for _score_value, i, j, tier in candidates:
        if i in used_truth or j in used_got:
            continue
        used_truth.add(i)
        used_got.add(j)
        result.pairs.append(
            Pair(truth=truth[i], got=extracted[j], tier=tier, truth_index=i, got_index=j)
        )

    result.pairs.sort(key=lambda p: p.truth_index)
    result.unmatched_truth = [t for i, t in enumerate(truth) if i not in used_truth]
    result.spurious = [g for j, g in enumerate(extracted) if j not in used_got]
    return result
