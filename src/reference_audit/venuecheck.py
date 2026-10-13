"""Bounded venue evidence. Missing search hits and another version's venue prove no absence."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from anyascii import anyascii

from reference_audit.matching.features import is_repository_venue, title_ratio
from reference_audit.matching.names import author_overlap
from reference_audit.models import (
    BibEntry, FieldFinding, SourceRecord, VenueCatalogue, VenueEvidence,
)
from reference_audit.parsing.identifiers import extract_arxiv_id


def _fold(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", anyascii(value).lower())).strip()


def _published_doi(value: str | None) -> str:
    doi = (value or "").lower()
    return "" if doi.startswith("10.48550/arxiv") else doi


def specific_artifact_evidence(entry: BibEntry, record: SourceRecord) -> VenueEvidence | None:
    """A publisher/registrant record bound to the cited published DOI proves its container.

    Another DOI, a pooled value or an arXiv DOI can name another version. Its venue cannot
    establish that the work never appeared at the claimed venue.
    """
    doi = _published_doi(entry.ids.doi)
    if (not doi or not record.venue.strip() or record.members
            or record.source not in {"publisher", "crossref"}
            or record.is_preprint or is_repository_venue(record.venue)
            or _published_doi(record.ids.doi) != doi):
        return None
    return VenueEvidence(
        kind="specific_artifact", source_urls=[f"https://doi.org/{doi}"],
        scope=f"the published artifact identified by DOI {doi}",
    )


def _scope_covers_claim(entry: BibEntry, catalogue: VenueCatalogue) -> bool:
    # Ambiguous aliases do not establish negative coverage.
    if entry.year != catalogue.year or _fold(entry.venue) != _fold(catalogue.venue):
        return False
    if catalogue.scope == "all_publications":
        return True
    named = _fold(entry.venue)
    if "workshop" in named:
        return False
    return "main conference" in named or "proceedings" in named or (
        "advances in neural information processing systems" in named
    )


def catalogue_venue_check(
    entry: BibEntry, records: list[SourceRecord], catalogue: VenueCatalogue,
) -> FieldFinding | None:
    """Absence requires a complete, scoped inventory with no possible identity match.

    Similar titles, identifier links or matching authors can indicate a renamed/versioned work;
    they block a negative claim and are not silently treated as confirmed matches.
    """
    if not _scope_covers_claim(entry, catalogue):
        return None
    if (not catalogue.complete or len(catalogue.entries) != catalogue.expected_count
            or len({item.model_dump_json() for item in catalogue.entries}) != len(catalogue.entries)
            or not all(urlparse(u).scheme in {"http", "https"} and urlparse(u).netloc
                       for u in (catalogue.source_url, catalogue.completeness_url))):
        return None
    targets = [entry, *records]
    if not entry.title or not entry.authors:
        return None
    for item in catalogue.entries:
        for target in targets:
            doi_equal = bool(target.ids.doi and item.ids.doi and
                             target.ids.doi.lower() == item.ids.doi.lower())
            target_arxiv = extract_arxiv_id(target.ids.arxiv_id, fallback_text=target.ids.doi)
            item_arxiv = extract_arxiv_id(item.ids.arxiv_id, fallback_text=item.ids.doi)
            arxiv_equal = bool(target_arxiv and target_arxiv == item_arxiv)
            close_title = title_ratio(target.title, item.title) >= 0.8
            close_authors = bool(target.authors and item.authors and
                                 author_overlap(target.authors, item.authors) >= 0.8)
            if doi_equal or arxiv_equal or close_title or close_authors:
                return None
    scope = f"{catalogue.venue}, {catalogue.year}, {catalogue.scope}"
    evidence = VenueEvidence(
        kind="complete_catalogue", source_urls=[catalogue.source_url, catalogue.completeness_url],
        scope=scope,
    )
    return FieldFinding(
        field="journal/venue", bib_value=entry.venue, status="error",
        sources=[catalogue.source_url], venue_evidence=evidence,
        detail=(f"the complete publication catalogue for {scope} ({catalogue.expected_count} entries) "
                "contains no matching work after checking identifiers, known title variants and "
                "authors; this finding concerns that catalogue's scope only"),
    )
