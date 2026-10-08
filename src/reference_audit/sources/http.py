"""Shared async HTTP utilities: rate limiting + transient-error retry.

Modeled on `sciwrite-lint/rate_limiter.py`. `error` from a transport failure or 429/5xx is
surfaced distinctly so the pipeline never treats an outage as 'not found'.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException as ImpersonateRequestError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

DEFAULT_TIMEOUT = 30.0
DEFAULT_USER_AGENT = "reference-audit/0.1 (https://github.com/; mailto:reference-audit@example.org)"

# Some publisher platforms (Silverchair direct.mit.edu, Atypon journals.sagepub.com) sit behind
# Cloudflare, which fingerprints the TLS ClientHello (JA3/JA4). httpx's fingerprint is flagged as a
# bot and 403'd even with a full browser header set + HTTP/2; curl/Firefox pass. curl_cffi replays a
# real browser's ClientHello, so the export resolves. Used ONLY for those publisher fetches —
# the clean-API sources stay on httpx.
DEFAULT_IMPERSONATE = "chrome"


class TransientHTTPError(Exception):
    """A retryable failure: transport error or 429/5xx response."""


class MonotonicRateLimiter:
    """Token-free min-interval limiter shared across coroutines (monotonic clock), with an optional
    cap on requests in flight.

    The interval spaces request *starts*, which alone does not bound concurrency: a source whose
    searches take seconds (Crossref) piles up dozens of open requests at 10 starts/s and answers 429.
    `max_in_flight` caps them; a slot is held for a whole request, retries and backoff included.
    """

    def __init__(self, rate_per_sec: float, max_in_flight: int | None = None):
        self._min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._last = 0.0
        self._lock = asyncio.Lock()
        self.max_in_flight = max_in_flight
        self._slots = asyncio.Semaphore(max_in_flight) if max_in_flight else None

    async def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._last + self._min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one in-flight slot for the duration of a request (a no-op without a cap)."""
        if self._slots is None:
            yield
            return
        async with self._slots:
            yield


def new_client(user_agent: str = DEFAULT_USER_AGENT, timeout: float = DEFAULT_TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=True)


def is_transient_status(resp: httpx.Response) -> bool:
    """The default definition of a retryable response: rate-limited, or a server-side failure."""
    return resp.status_code == 429 or resp.status_code >= 500


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    retry=retry_if_exception_type(TransientHTTPError),
    reraise=True,
)
async def _send_with_retry(
    send: Callable[[], Awaitable[httpx.Response]],
    transient: Callable[[httpx.Response], bool] = is_transient_status,
) -> httpx.Response:
    """Verb-agnostic retry core: one definition of "what is transient" for every request we make.

    `transient` is injectable because not every 5xx means "the server is unwell". GROBID reports a
    *per-document* parse failure through a 500 body, and retrying that three times is pure latency
    while reporting it as an outage would mislabel an unparseable PDF as a service problem.
    """
    try:
        resp = await send()
    except httpx.TransportError as exc:  # network/DNS/timeout
        raise TransientHTTPError(f"transport: {exc}") from exc
    if transient(resp):
        raise TransientHTTPError(f"http {resp.status_code}")
    return resp


async def _request_with_retry(
    client: httpx.AsyncClient,
    limiter: MonotonicRateLimiter,
    url: str,
    params: dict | None,
    headers: dict | None,
) -> httpx.Response:
    """Every attempt, retries included, waits its turn at the limiter. The in-flight slot is held
    across the backoff, so a source that answered 429 is not handed the next request meanwhile."""

    async def send() -> httpx.Response:
        await limiter.acquire()
        return await client.get(url, params=params, headers=headers)

    async with limiter.slot():
        return await _send_with_retry(send)


async def post_multipart(
    client: httpx.AsyncClient,
    url: str,
    *,
    files: dict,
    data: dict | None = None,
    timeout: float | None = None,
    transient: Callable[[httpx.Response], bool] = is_transient_status,
) -> httpx.Response:
    """Multipart POST under the same transient-retry policy as the GET helpers.

    Returns the raw response — unlike `get_json`/`get_text` the caller classifies non-2xx itself,
    because a multipart-POST service (GROBID) encodes distinct per-request failure modes in the body
    and they need different handling. Not rate-limited: the only caller talks to a single-tenant local
    service one request per run.
    """
    return await _send_with_retry(
        lambda: client.post(url, files=files, data=data, timeout=timeout), transient
    )


async def get_json(
    client: httpx.AsyncClient,
    limiter: MonotonicRateLimiter,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, dict | None]:
    """Rate-limited GET with retry. Returns (status_code, json-or-None).

    Raises TransientHTTPError after exhausting retries (caller maps to SourceQueryResult.error).
    A 404 returns (404, None) — a genuine 'absent', distinct from an error.
    """
    resp = await _request_with_retry(client, limiter, url, params, headers)
    if resp.status_code == 404:
        return 404, None
    if resp.status_code >= 400:
        # non-retryable client error (e.g. 400/403) — treat as a hard error, not 'absent'
        raise TransientHTTPError(f"http {resp.status_code}")
    try:
        return resp.status_code, resp.json()
    except ValueError as exc:
        raise TransientHTTPError(f"invalid json: {exc}") from exc


async def get_text(
    client: httpx.AsyncClient,
    limiter: MonotonicRateLimiter,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, str]:
    """Rate-limited GET with retry for text/XML payloads (e.g. the arXiv Atom API).

    Mirrors `get_json`: exponential backoff on 429/5xx via `_request_with_retry`, then raises
    TransientHTTPError after exhausting retries. A 404 returns (404, "").
    """
    resp = await _request_with_retry(client, limiter, url, params, headers)
    if resp.status_code == 404:
        return 404, ""
    if resp.status_code >= 400:
        # non-retryable client error (e.g. 400/403) — treat as a hard error, not 'absent'
        raise TransientHTTPError(f"http {resp.status_code}")
    return resp.status_code, resp.text


async def get_html(
    client: httpx.AsyncClient,
    limiter: MonotonicRateLimiter,
    url: str,
    *,
    headers: dict | None = None,
) -> tuple[int, str, str]:
    """Rate-limited GET of an HTML page, returning (status, final_url, text).

    Unlike `get_text`, the *final* URL (after redirects) is returned too, so a web-artifact check can
    record where the citation actually landed. A dead link — HTTP 404 or 410 (Gone) — returns
    (status, final_url, "") so the caller can report it as a dead link rather than an outage; any
    other 4xx (e.g. a 403 bot-wall) raises TransientHTTPError so it is reported as a block, never a
    false 'page absent'.
    """
    resp = await _request_with_retry(client, limiter, url, None, headers)
    if resp.status_code in (404, 410):
        return resp.status_code, str(resp.url), ""
    if resp.status_code >= 400:
        raise TransientHTTPError(f"http {resp.status_code}")
    return resp.status_code, str(resp.url), resp.text


def new_impersonate_session(timeout: float = DEFAULT_TIMEOUT) -> AsyncSession:
    """A browser-TLS-impersonating session (libcurl via curl_cffi) for Cloudflare-fingerprint-walled
    endpoints. Reused across calls so the issued `__cf_bm` cookie persists. See DEFAULT_IMPERSONATE.
    """
    return AsyncSession(timeout=timeout, impersonate=DEFAULT_IMPERSONATE)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    retry=retry_if_exception_type(TransientHTTPError),
    reraise=True,
)
async def _impersonate_get_with_retry(
    session: AsyncSession, limiter: MonotonicRateLimiter, url: str
):
    await limiter.acquire()  # per attempt, as in `_request_with_retry`
    try:
        resp = await session.get(url)
    except ImpersonateRequestError as exc:  # network/DNS/timeout/TLS
        raise TransientHTTPError(f"transport: {exc}") from exc
    if resp.status_code == 429 or resp.status_code >= 500:
        raise TransientHTTPError(f"http {resp.status_code}")
    return resp


async def get_text_impersonate(
    session: AsyncSession,
    limiter: MonotonicRateLimiter,
    url: str,
) -> tuple[int, str]:
    """Browser-impersonating counterpart of `get_text` for Cloudflare-fingerprint-walled publisher
    exports. Same contract: rate-limited, exponential backoff on transport/429/5xx, then raises
    TransientHTTPError after exhausting retries; a 404 returns (404, ""); any other 4xx (e.g. a 403
    bot-wall) raises TransientHTTPError so the caller reports a block rather than a false 'absent'.
    """
    async with limiter.slot():
        resp = await _impersonate_get_with_retry(session, limiter, url)
    if resp.status_code == 404:
        return 404, ""
    if resp.status_code >= 400:
        raise TransientHTTPError(f"http {resp.status_code}")
    return resp.status_code, resp.text
