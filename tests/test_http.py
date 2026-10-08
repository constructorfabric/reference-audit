"""Shared HTTP layer (`sources/http.py`): the in-flight cap, and retries that go through the limiter.

A source like Crossref limits requests in flight as well as their rate; spacing request starts alone
let a batch pile up dozens of open searches and turned a fifth of a HALLMARK chunk into 429s.
"""

import asyncio

import httpx
import pytest
from tenacity import wait_none

from reference_audit.sources import http
from reference_audit.sources.arxiv import ArxivAdapter
from reference_audit.sources.crossref import CrossrefAdapter
from reference_audit.sources.http import MonotonicRateLimiter, get_json


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(http, "_send_with_retry", http._send_with_retry.retry_with(wait=wait_none()))


class CountingLimiter(MonotonicRateLimiter):
    def __init__(self, max_in_flight=None):
        super().__init__(0, max_in_flight)
        self.acquired = 0

    async def acquire(self) -> None:
        self.acquired += 1
        await super().acquire()


async def test_in_flight_cap_bounds_concurrent_requests():
    in_flight = peak = 0

    async def handler(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return httpx.Response(200, json={})

    limiter = MonotonicRateLimiter(0, max_in_flight=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await asyncio.gather(*(get_json(client, limiter, f"https://x.test/{i}") for i in range(6)))
    assert peak == 2


async def test_every_retry_waits_its_turn_at_the_limiter(no_backoff):
    answers = iter([httpx.Response(429), httpx.Response(200, json={"ok": True})])

    async def handler(request):
        return next(answers)

    limiter = CountingLimiter()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        status, data = await get_json(client, limiter, "https://x.test/a")
    assert (status, data) == (200, {"ok": True})
    assert limiter.acquired == 2  # the retry was rate-limited too, not fired past the limiter


async def test_slot_is_held_across_a_retry(no_backoff):
    seen: list[str] = []
    first_a = True

    async def handler(request):
        nonlocal first_a
        seen.append(request.url.path)
        if request.url.path == "/a" and first_a:
            first_a = False
            return httpx.Response(429)
        return httpx.Response(200, json={})

    limiter = MonotonicRateLimiter(0, max_in_flight=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await asyncio.gather(
            get_json(client, limiter, "https://x.test/a"),
            get_json(client, limiter, "https://x.test/b"),
        )
    # The throttled request retries before the queued one is sent, never interleaved with it.
    assert seen == ["/a", "/a", "/b"]


async def test_documented_source_caps_reach_the_limiter():
    for adapter, cap in ((CrossrefAdapter(), 3), (ArxivAdapter(), 1)):
        assert adapter.rate_limiter.max_in_flight == cap
        await adapter.aclose()
    assert ArxivAdapter.rate_per_sec <= 1 / 3  # arXiv API terms: one request every three seconds
