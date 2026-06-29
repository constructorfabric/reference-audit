"""Adapter construction + per-entry routing (entry_type × available identifiers).

Builds the default adapter set from config, and decides which adapters to query by id vs by
metadata for a given entry. Metadata search ALWAYS runs (even when an id is present) so it can
backfill missing DOIs/ISBNs and supply corroborating candidates for the equivalence step.
"""

from __future__ import annotations

from dataclasses import dataclass

from reference_audit.config import AuditConfig
from reference_audit.models import BibEntry, EntryType
from reference_audit.sources.arxiv import ArxivAdapter
from reference_audit.sources.base import SourceAdapter
from reference_audit.sources.crossref import CrossrefAdapter
from reference_audit.sources.google_books import GoogleBooksAdapter
from reference_audit.sources.openalex import OpenAlexAdapter
from reference_audit.sources.openlibrary import OpenLibraryAdapter
from reference_audit.sources.publisher import PublisherAdapter
from reference_audit.sources.render import ChromiumRenderer, find_browser
from reference_audit.sources.semantic_scholar import SemanticScholarAdapter
from reference_audit.sources.web import WebAdapter

# Reputable venues that legitimately mint no DOI — a no-DOI match here is not a defect (README 1.1).
VENUE_ALLOWLIST_NO_DOI = ("tmlr", "transactions on machine learning research")


def build_web_renderer(config: AuditConfig) -> ChromiumRenderer | None:
    """A headless renderer for SPA shells, or None when rendering is disabled. The renderer self-
    reports 'unavailable' at call time when no browser is found, so the funnel leaves such pages
    unresolved rather than crying hallucination (see `sources.render`)."""
    if not config.web_render_enabled:
        return None
    return ChromiumRenderer(
        find_browser(config.web_render_browser_path),
        timeout=config.web_render_timeout,
        virtual_time_ms=config.web_render_virtual_time_ms,
    )


def build_default_adapters(config: AuditConfig) -> list[SourceAdapter]:
    mailto = config.resolved_mailto()
    return [
        CrossrefAdapter(mailto=mailto),
        OpenAlexAdapter(mailto=mailto),
        SemanticScholarAdapter(api_key=config.s2_api_key),
        ArxivAdapter(),
        OpenLibraryAdapter(email=config.openlibrary_email),
        GoogleBooksAdapter(api_key=config.google_books_api_key),
        PublisherAdapter(),
        WebAdapter(render=build_web_renderer(config)),
    ]


@dataclass
class Route:
    id_adapters: list[SourceAdapter]
    metadata_adapters: list[SourceAdapter]


def route_entry(entry: BibEntry, adapters: list[SourceAdapter]) -> Route:
    """Pick adapters to query by-id and by-metadata for this entry."""
    by_name = {a.name: a for a in adapters}

    def present(*names: str) -> list[SourceAdapter]:
        return [by_name[n] for n in names if n in by_name]

    id_adapters: list[SourceAdapter] = []
    if entry.ids.doi:
        id_adapters += present("crossref", "openalex", "semantic_scholar")
    if entry.ids.arxiv_id:
        id_adapters += present("arxiv", "openalex", "semantic_scholar")
    if entry.ids.isbn13:
        id_adapters += present("openlibrary", "google_books")
    # A cited Google Books volume id (books.google.…/books?id=…) resolves to exactly that volume —
    # the authoritative key for a trade/book title both the article-centric search and Open
    # Library's strict title match miss.
    if entry.ids.google_books:
        id_adapters += present("google_books")
    # A cited OpenAlex Work id resolves to exactly that Work — the authoritative key for entries
    # (notably books/trade titles) the article-centric metadata search and Crossref/Open Library
    # miss. Routed for every entry type, since OpenAlex indexes books too.
    if entry.ids.openalex:
        id_adapters += present("openalex")
    # NOTE: `publisher` (the DOI landing-page citation export) is deliberately NOT an identity
    # source. It is queried only in the advisory enrichment pass — a bot-walled/blocked publisher
    # must never set the `errored` flag and mask a hallucinated DOI as 'unresolved' vs 'no match'.

    if entry.entry_type in (EntryType.BOOK, EntryType.INCOLLECTION):
        metadata_adapters = present("openlibrary", "google_books", "crossref")
    elif entry.entry_type == EntryType.MISC:
        metadata_adapters = present("arxiv", "openalex", "crossref", "semantic_scholar")
    else:  # ARTICLE / INPROCEEDINGS / UNKNOWN
        metadata_adapters = present("crossref", "openalex", "semantic_scholar")

    # de-dup while preserving order
    id_adapters = list(dict.fromkeys(id_adapters))
    return Route(id_adapters=id_adapters, metadata_adapters=metadata_adapters)


def venue_allows_no_doi(venue: str) -> bool:
    v = (venue or "").lower()
    return any(allowed in v for allowed in VENUE_ALLOWLIST_NO_DOI)
