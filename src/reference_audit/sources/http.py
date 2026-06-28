"""Shared async HTTP utilities: rate limiting + transient-error retry.

Modeled on `sciwrite-lint/rate_limiter.py`. `error` from a transport failure or 429/5xx is
surfaced distinctly so the pipeline never treats an outage as 'not found'.
"""

from __future__ import annotations

import asyncio
import time

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
    """Token-free min-interval limiter shared across coroutines (monotonic clock)."""

    def __init__(self, rate_per_sec: float):
        self._min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._last + self._min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


def new_client(user_agent: str = DEFAULT_USER_AGENT, timeout: float = DEFAULT_TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=True)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    retry=retry_if_exception_type(TransientHTTPError),
    reraise=True,
)
async def _request_with_retry(
    client: httpx.AsyncClient, url: str, params: dict | None, headers: dict | None
) -> httpx.Response:
    try:
        resp = await client.get(url, params=params, headers=headers)
    except httpx.TransportError as exc:  # network/DNS/timeout
        raise TransientHTTPError(f"transport: {exc}") from exc
    if resp.status_code == 429 or resp.status_code >= 500:
        raise TransientHTTPError(f"http {resp.status_code}")
    return resp


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
    await limiter.acquire()
    resp = await _request_with_retry(client, url, params, headers)
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
    await limiter.acquire()
    resp = await _request_with_retry(client, url, params, headers)
    if resp.status_code == 404:
        return 404, ""
    if resp.status_code >= 400:
        # non-retryable client error (e.g. 400/403) — treat as a hard error, not 'absent'
        raise TransientHTTPError(f"http {resp.status_code}")
    return resp.status_code, resp.text


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
async def _impersonate_get_with_retry(session: AsyncSession, url: str):
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
    await limiter.acquire()
    resp = await _impersonate_get_with_retry(session, url)
    if resp.status_code == 404:
        return 404, ""
    if resp.status_code >= 400:
        raise TransientHTTPError(f"http {resp.status_code}")
    return resp.status_code, resp.text
