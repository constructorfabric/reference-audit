"""Web-artifact resolver for URL-only ``@misc`` references (a blog post, software/project page,
dataset, documentation page — e.g. ``mordvintsev2022particle``).

A URL-only ``@misc`` names no scholarly identifier, so the DOI/ISBN/arXiv matcher cannot reach it
and the scholarly databases do not index it. This adapter instead fetches the cited page itself and
extracts the metadata the page declares about itself in its ``<head>`` — Open Graph
(``og:*``), Twitter cards, Highwire/Google-Scholar ``citation_*``, Dublin Core (``dc.*``), the
``<title>``, and ``<meta name="author">`` — so the page's *own* claimed title/authors can be checked
against the ``.bib`` entry (the deterministic step 2), with the page text kept for an LLM fallback
(step 3, in `matching.webcheck`).

Reliability-first (per the project's two-level reporting goal):
  * a transport failure, timeout, or bot-wall (a non-404 4xx / 5xx) is surfaced as an ``error`` —
    reported, retried next run, never read as 'the page says no' and never cached;
  * a 404/410 is a *dead link* — a real, cacheable finding, returned as a record flagged ``dead``;
  * a live page with no usable metadata yields an empty-titled record, so the caller escalates to
    the LLM rather than guessing a verdict from absence.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from reference_audit.models import (
    EntryType,
    Identifiers,
    SourceQueryResult,
    SourceRecord,
)
from reference_audit.sources.base import SourceAdapter
from reference_audit.sources.http import TransientHTTPError, get_html
from reference_audit.sources.render import RenderError, RenderUnavailable

# Metadata keys carrying the page's self-declared title, in preference order. Keys are matched
# case-folded against either a <meta> tag's `property` or its `name` attribute.
_TITLE_KEYS = ("og:title", "twitter:title", "citation_title", "dc.title", "dcterms.title")
# Author keys; several tags may repeat (one <meta> per author) — all are collected.
_AUTHOR_KEYS = ("citation_author", "article:author", "author", "dc.creator", "dcterms.creator")
_DESC_KEYS = ("og:description", "description", "twitter:description", "dc.description")
_SITE_KEYS = ("og:site_name", "application-name")
_DATE_KEYS = (
    "citation_publication_date",
    "citation_date",
    "article:published_time",
    "dc.date",
    "dcterms.date",
    "og:updated_time",
)

# Cap the visible-text slice we carry for the LLM fallback (the model never needs the whole page).
_TEXT_LIMIT = 8000
_YEAR_RE = re.compile(r"(1[5-9]\d{2}|20\d{2}|21\d{2})")

# Below this much visible text a 200 page is suspected to be an un-rendered single-page-app shell
# (a spinner + a script bundle, with the real content injected only after JavaScript runs).
_SHELL_TEXT_THRESHOLD = 200
# Markers that, on an otherwise text-empty page, identify a client-side-rendered app shell.
_SPA_MARKERS = (
    'id="app"',
    "id='app'",
    'id="root"',
    "id='root'",
    "<app-root",
    "ng-version",
    "data-reactroot",
    "__nuxt__",
    'type="module"',
    "enable javascript",
    "requires javascript",
)


def _looks_unrendered(html: str, record: SourceRecord) -> bool:
    """True when a 200 page carries almost no readable text yet shows single-page-app markers — i.e.
    the served HTML is an empty shell and the real content loads only via JavaScript. Gating on the
    near-empty text keeps ordinary content pages (which legitimately use modules/roots) out."""
    text = (record.raw or {}).get("text") or ""
    if len(text) >= _SHELL_TEXT_THRESHOLD:
        return False
    low = html.lower()
    return any(marker in low for marker in _SPA_MARKERS)


def _collect_meta(soup: BeautifulSoup) -> dict[str, list[str]]:
    """Map each case-folded meta key (property OR name) to its non-empty content values, in order."""
    by_key: dict[str, list[str]] = {}
    for tag in soup.find_all("meta"):
        content = (tag.get("content") or "").strip()
        if not content:
            continue
        key = (tag.get("property") or tag.get("name") or "").strip().lower()
        if key:
            by_key.setdefault(key, []).append(content)
    return by_key


def _first(by_key: dict[str, list[str]], keys: tuple[str, ...]) -> str:
    for k in keys:
        if by_key.get(k):
            return by_key[k][0]
    return ""


def _all(by_key: dict[str, list[str]], keys: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for k in keys:
        out.extend(by_key.get(k, []))
    # de-dup while preserving order (the same author can appear under several keys)
    seen: set[str] = set()
    deduped = []
    for a in out:
        if a not in seen:
            seen.add(a)
            deduped.append(a)
    return deduped


def _meta_year(by_key: dict[str, list[str]]) -> int | None:
    raw = _first(by_key, _DATE_KEYS)
    m = _YEAR_RE.search(raw)
    return int(m.group(1)) if m else None


def extract_web_metadata(html: str, final_url: str) -> SourceRecord:
    """Parse a fetched HTML page into a `SourceRecord` describing what the page says about itself.

    Title falls back to the ``<title>`` element when no metadata title is present; an empty title
    signals the caller to escalate to the LLM (the page declared nothing checkable).
    """
    soup = BeautifulSoup(html or "", "html.parser")
    by_key = _collect_meta(soup)

    title = _first(by_key, _TITLE_KEYS)
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    authors = _all(by_key, _AUTHOR_KEYS)
    description = _first(by_key, _DESC_KEYS)
    site_name = _first(by_key, _SITE_KEYS)
    year = _meta_year(by_key)

    for junk in soup(["script", "style", "noscript", "template"]):
        junk.decompose()
    text = soup.get_text(" ", strip=True)[:_TEXT_LIMIT]

    return SourceRecord(
        source="web",
        source_native_id=final_url,
        title=" ".join(title.split()),
        authors=authors,
        year=year,
        ids=Identifiers(url=final_url),
        raw={
            "status": 200,
            "dead": False,
            "description": description,
            "site_name": site_name,
            "text": text,
        },
    )


def _dead_record(url: str, status: int) -> SourceRecord:
    return SourceRecord(
        source="web",
        source_native_id=url,
        ids=Identifiers(url=url),
        raw={"status": status, "dead": True},
    )


class WebAdapter(SourceAdapter):
    """Fetch the cited URL of a web ``@misc`` and extract its self-declared HTML metadata.

    Not a scholarly source: it is queried only on the explicit web-check path (never via
    `route_entry`'s by-id/by-metadata gathering), so its `lookup_by_id`/`search_by_metadata` stay
    the inherited no-ops. `fetch=...` injects a stub fetcher (async ``(url) -> (status, final_url,
    html)``) for tests, mirroring `PublisherAdapter`'s `export_fetch`.
    """

    name = "web"
    handles = {EntryType.MISC}
    rate_per_sec = 2.0  # be gentle on arbitrary third-party sites

    def __init__(self, *, fetch=None, render=None, **kwargs):
        super().__init__(**kwargs)
        self._fetch = fetch
        # `render` is an async ``(url) -> (status, final_url, html)`` that runs the page in a headless
        # browser; None means rendering is not configured (a shell is then left unresolved, never
        # read as 'a different page'). Injected as a stub in tests, wired to `render.ChromiumRenderer`
        # in production. See `sources.render`.
        self._render = render

    async def _get(self, url: str) -> tuple[int, str, str]:
        if self._fetch is not None:
            return await self._fetch(url)
        return await get_html(self.client, self.rate_limiter, url)

    async def fetch_page(self, url: str) -> SourceQueryResult:
        if not url:
            return SourceQueryResult(source=self.name, query_kind="web", records=[])
        try:
            status, final_url, html = await self._get(url)
        except TransientHTTPError as exc:
            # Transport error / timeout / bot-wall (403) / 5xx after retries — a human-reachable page
            # we could not auto-fetch. Reported as an error, retried next run, never read as 'absent'.
            return SourceQueryResult(
                source=self.name,
                query_kind="web",
                error=f"could not fetch the cited page ({exc}): {url}",
            )
        landed = final_url or url
        if status in (404, 410):
            return SourceQueryResult(
                source=self.name, query_kind="web", records=[_dead_record(landed, status)]
            )

        record = extract_web_metadata(html, landed)
        if _looks_unrendered(html, record):
            # The served HTML is a client-side-rendered shell — the real content loads only via JS.
            # Render it in a headless browser so the page can be read like any other; a render
            # failure becomes an error (retried, uncached), and 'no browser' / 'still empty' is
            # marked so the funnel leaves the entry unresolved rather than crying hallucination.
            rendered = await self._maybe_render(url, landed)
            if isinstance(rendered, SourceQueryResult):
                return rendered  # a render *error* — propagate (uncached, retried next run)
            record = rendered
        else:
            record.raw["render"] = "not_needed"
        return SourceQueryResult(source=self.name, query_kind="web", records=[record])

    async def _maybe_render(self, url: str, landed: str) -> SourceRecord | SourceQueryResult:
        """Render an SPA shell. Returns the rendered `SourceRecord`, or a `SourceQueryResult` error
        when the render itself failed (so the caller propagates it as a retryable error, not a page).
        The returned record carries ``raw['render']``: ``rendered`` (read successfully), ``unavailable``
        (no browser configured), or ``rendered_empty`` (still a shell even after rendering)."""
        if self._render is None:
            shell = extract_web_metadata("", landed)
            shell.raw["render"] = "unavailable"
            return shell
        try:
            status, r_final, r_html = await self._render(url)
        except RenderUnavailable:
            shell = extract_web_metadata("", landed)
            shell.raw["render"] = "unavailable"
            return shell
        except RenderError as exc:
            return SourceQueryResult(
                source=self.name,
                query_kind="web",
                error=f"cited page needs a browser to render and rendering failed ({exc}): {url}",
            )
        record = extract_web_metadata(r_html, r_final or landed)
        record.raw["render"] = "rendered_empty" if _looks_unrendered(r_html, record) else "rendered"
        return record
