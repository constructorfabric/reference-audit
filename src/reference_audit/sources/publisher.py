"""Publisher-of-record resolver — the DOI's landing-page citation export.

The authority of record. Aggregators (Crossref/OpenAlex/Semantic Scholar) sometimes lack fields the
publisher exposes only through its own "Cite" export — notably pages/article numbers for
article-numbered venues (the MIT Press/Silverchair proceedings whose Crossref `page` is null but
whose BibTeX export carries `pages={131}`).

Resolution is deliberately conservative and reliability-first:
  1. dereference the DOI to its landing URL (a redirect — works even when the page body is
     bot-protected, because we read only the final URL);
  2. derive the platform's citation-export URL (Silverchair: `/Citation/Download?...` keyed by the
     numeric resource id in the landing path);
  3. fetch + parse the BibTeX.
The export fetch (step 3) goes through a browser-TLS-impersonating client (`get_text_impersonate`),
because these platforms sit behind Cloudflare, which fingerprints httpx's TLS ClientHello and 403s it
even with browser headers; replaying a real browser's ClientHello passes. If any step fails — unknown
platform, an escalated JS/interactive bot-challenge instead of BibTeX, a transport error — the result
is a reported gap (empty records, or an `error` for a transient block), never a guess and never
cached as "absent".
"""

from __future__ import annotations

from urllib.parse import quote, urlparse

import httpx

from reference_audit.models import EntryType, Identifiers, SourceQueryResult
from reference_audit.sources.base import SourceAdapter
from reference_audit.sources.http import (
    TransientHTTPError,
    get_text_impersonate,
    new_impersonate_session,
)
from reference_audit.sources.normalize import publisher_bibtex_to_record

_DOI_RESOLVER = "https://doi.org/"

# Silverchair-hosted publishers expose a citation download at
#   {scheme}://{host}/Citation/Download?resourceId={N}&resourceType=3&citationFormat=2  (BibTeX)
# where {N} is the trailing numeric segment of the article landing path
#   .../isal2023/35/131/116921  ->  resourceId=116921
_SILVERCHAIR_HOSTS = ("direct.mit.edu",)

# Atypon-hosted publishers (Sage, …) expose a citation download keyed directly by the DOI:
#   {scheme}://{host}/action/downloadCitation?doi={DOI}&format=bibtex&include=cit  (BibTeX)
# Unlike Silverchair, no landing-path resource id is needed — the DOI alone derives the URL.
# Like Silverchair, these endpoints are Cloudflare-walled, so the export fetch goes through the
# browser-impersonating client; an *escalated* interactive challenge still surfaces a reported error.
_ATYPON_HOSTS = ("journals.sagepub.com",)


def _silverchair_export_url(landing: str) -> str | None:
    parts = urlparse(landing)
    if not any(parts.netloc == h or parts.netloc.endswith("." + h) for h in _SILVERCHAIR_HOSTS):
        return None
    segments = [s for s in parts.path.split("/") if s]
    if not segments or not segments[-1].isdigit():
        return None
    return (
        f"{parts.scheme}://{parts.netloc}/Citation/Download"
        f"?resourceId={segments[-1]}&resourceType=3&citationFormat=2"
    )


def _atypon_export_url(landing: str, doi: str) -> str | None:
    parts = urlparse(landing)
    if not any(parts.netloc == h or parts.netloc.endswith("." + h) for h in _ATYPON_HOSTS):
        return None
    return (
        f"{parts.scheme}://{parts.netloc}/action/downloadCitation"
        f"?doi={quote(doi, safe='')}&format=bibtex&include=cit"
    )


class PublisherAdapter(SourceAdapter):
    """Fetch the DOI publisher's own citation export (Silverchair and Atypon platforms)."""

    name = "publisher"
    handles = {
        EntryType.ARTICLE,
        EntryType.INPROCEEDINGS,
        EntryType.BOOK,
        EntryType.INCOLLECTION,
        EntryType.MISC,
    }
    rate_per_sec = 2.0  # be gentle on publisher sites

    def __init__(self, *, export_fetch=None, **kwargs):
        """`export_fetch` (async `(url) -> (status, text)`) overrides the citation-export fetcher;
        injected by tests. In production it defaults to a lazily-created browser-impersonating
        session (curl_cffi), kept separate from `self.client` (httpx) which still serves the DOI
        resolution. DOI resolution stays on httpx because it reads only the redirect target, never a
        Cloudflare-walled body.
        """
        super().__init__(**kwargs)
        self._export_fetch = export_fetch
        self._imp_session = None

    async def _fetch_export(self, url: str) -> tuple[int, str]:
        if self._export_fetch is not None:
            return await self._export_fetch(url)
        if self._imp_session is None:
            self._imp_session = new_impersonate_session()
        return await get_text_impersonate(self._imp_session, self.rate_limiter, url)

    async def aclose(self) -> None:
        if self._imp_session is not None:
            await self._imp_session.close()
        await super().aclose()

    async def _landing_url(self, doi: str) -> str:
        """Final URL the DOI resolves to (redirect only; the page body may be bot-protected)."""
        await self.rate_limiter.acquire()
        try:
            resp = await self.client.get(f"{_DOI_RESOLVER}{doi}")
        except httpx.TransportError as exc:
            raise TransientHTTPError(f"transport: {exc}") from exc
        return str(resp.url)

    async def doi_registered(self, doi: str) -> bool | None:
        """Whether the handle system (doi.org) actually knows this DOI.

        A *backfilled* DOI is only useful if it resolves: authors sometimes record a bogus DOI in
        their arXiv/preprint metadata (e.g. an ACM `10.5555/...` placeholder for a venue that mints
        no real DOI) which the aggregators then echo. doi.org answers authoritatively — a redirect to
        a landing page (handle found) ⇒ registered; HTTP 404 (DOI Not Found) ⇒ not registered.
        Redirects are deliberately NOT followed, so we read doi.org's own verdict and never mistake a
        bot-walled destination (403) for an unregistered DOI. Returns None when doi.org could not be
        reached (transport, or any non-404 error) — an outage is never read as 'invalid'.
        """
        await self.rate_limiter.acquire()
        try:
            resp = await self.client.get(f"{_DOI_RESOLVER}{doi}", follow_redirects=False)
        except httpx.HTTPError:
            return None
        if resp.is_redirect or 200 <= resp.status_code < 300:
            return True
        if resp.status_code == 404:
            return False
        return None  # 401/403/429/5xx etc. — undetermined; never asserted invalid

    async def lookup_by_id(self, ids: Identifiers) -> SourceQueryResult:
        if not ids.doi:
            return SourceQueryResult(source=self.name, query_kind="id", records=[])
        try:
            landing = await self._landing_url(ids.doi)
        except TransientHTTPError as exc:
            return SourceQueryResult(source=self.name, query_kind="id", error=str(exc))

        export_url = _silverchair_export_url(landing) or _atypon_export_url(landing, ids.doi)
        if export_url is None:
            # Unknown publisher platform — nothing we know how to fetch. A clean "not found" here
            # (not an error): field checks then report the gap against the sources we did consult.
            return SourceQueryResult(source=self.name, query_kind="id", records=[])

        try:
            status, text = await self._fetch_export(export_url)
        except TransientHTTPError as exc:
            # Most commonly a Cloudflare/JS bot-challenge (HTTP 403). Surfaced as an error — a
            # human-retrievable datum we could not auto-fetch — so it is reported, not treated as
            # absent, and retried next run rather than cached.
            return SourceQueryResult(
                source=self.name,
                query_kind="id",
                error=f"publisher citation export not retrievable ({exc}): {export_url}",
            )
        if status == 404:
            return SourceQueryResult(source=self.name, query_kind="id", records=[])

        record = publisher_bibtex_to_record(text)
        if record is None:
            return SourceQueryResult(
                source=self.name,
                query_kind="id",
                error=f"publisher response was not a citation export (bot-challenge?): {export_url}",
            )
        return SourceQueryResult(source=self.name, query_kind="id", records=[record])
