"""GrobidClient failure taxonomy, over mocked HTTP. No container needed.

Every branch here exists because it produces a *different* message for the user, so each test pins
what they are told, not just which exception class fires.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from reference_audit.pdf.grobid import (
    FULLTEXT_PARAMS,
    GROBID_IMAGE,
    GrobidClient,
    GrobidRequestError,
    GrobidUnavailableError,
    PdfUnparseableError,
    _timeout_for,
)

BASE = "http://grobid.test:8070"
ALIVE = f"{BASE}/api/isalive"
FULLTEXT = f"{BASE}/api/processFulltextDocument"

_TEI_OK = '<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body/><back/></text></TEI>'


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "paper.pdf"
    p.write_bytes(b"%PDF-1.7\n" + b"x" * 4096)
    return p


def _client() -> GrobidClient:
    return GrobidClient(BASE, client=httpx.AsyncClient())


@respx.mock
async def test_happy_path_returns_the_tei(pdf):
    respx.get(ALIVE).respond(200, text="true")
    route = respx.post(FULLTEXT).respond(200, text=_TEI_OK)
    client = _client()
    try:
        assert await client.fulltext_tei(pdf) == _TEI_OK
    finally:
        await client.aclose()
    assert route.call_count == 1


@respx.mock
async def test_pinned_request_params_are_sent(pdf):
    """These params are an extraction contract shared with the committed fixtures, not tuning."""
    respx.get(ALIVE).respond(200, text="true")
    route = respx.post(FULLTEXT).respond(200, text=_TEI_OK)
    client = _client()
    try:
        await client.fulltext_tei(pdf)
    finally:
        await client.aclose()
    body = route.calls[0].request.content.decode("latin-1")
    for name, value in FULLTEXT_PARAMS.items():
        assert f'name="{name}"' in body and value in body
    # The critical one: consolidation would let GROBID rewrite references from Crossref, repairing
    # the very defects this tool detects.
    assert FULLTEXT_PARAMS["consolidateCitations"] == "0"
    assert 'filename="paper.pdf"' in body


@respx.mock
async def test_unreachable_service_names_the_start_command(pdf):
    respx.get(ALIVE).mock(side_effect=httpx.ConnectError("connection refused"))
    client = _client()
    try:
        with pytest.raises(GrobidUnavailableError) as exc:
            await client.fulltext_tei(pdf)
    finally:
        await client.aclose()
    message = str(exc.value)
    assert BASE in message
    assert "podman run" in message and GROBID_IMAGE in message
    assert "-p 8070:8070" in message


@respx.mock
async def test_alive_non_200_is_reported_as_not_ready(pdf):
    respx.get(ALIVE).respond(503)
    client = _client()
    try:
        with pytest.raises(GrobidUnavailableError, match="not ready"):
            await client.fulltext_tei(pdf)
    finally:
        await client.aclose()


@respx.mock
async def test_no_upload_is_attempted_when_the_service_is_down(pdf):
    """Fail in seconds on a health check, not after a multi-minute upload timeout."""
    respx.get(ALIVE).respond(500)
    upload = respx.post(FULLTEXT).respond(200, text=_TEI_OK)
    client = _client()
    try:
        with pytest.raises(GrobidUnavailableError):
            await client.fulltext_tei(pdf)
    finally:
        await client.aclose()
    assert upload.call_count == 0


@respx.mock
async def test_persistent_5xx_retries_three_times_then_raises(pdf):
    respx.get(ALIVE).respond(200, text="true")
    route = respx.post(FULLTEXT).respond(502, text="Bad Gateway")
    client = _client()
    try:
        with pytest.raises(GrobidRequestError) as exc:
            await client.fulltext_tei(pdf)
    finally:
        await client.aclose()
    assert route.call_count == 3
    assert "podman logs grobid" in str(exc.value)


@respx.mock
async def test_transient_5xx_then_success(pdf):
    respx.get(ALIVE).respond(200, text="true")
    route = respx.post(FULLTEXT).mock(
        side_effect=[httpx.Response(503, text="warming up"), httpx.Response(200, text=_TEI_OK)]
    )
    client = _client()
    try:
        assert await client.fulltext_tei(pdf) == _TEI_OK
    finally:
        await client.aclose()
    assert route.call_count == 2


@respx.mock
async def test_unparseable_pdf_is_not_retried(pdf):
    """A per-document parse failure will not fix itself; retrying is pure latency."""
    respx.get(ALIVE).respond(200, text="true")
    route = respx.post(FULLTEXT).respond(500, text="[NO_BLOCKS] PDF parsing failed")
    client = _client()
    try:
        with pytest.raises(PdfUnparseableError) as exc:
            await client.fulltext_tei(pdf)
    finally:
        await client.aclose()
    assert route.call_count == 1
    assert exc.value.pdf_name == "paper.pdf"
    assert "paper.pdf" in str(exc.value)


@respx.mock
async def test_image_only_pdf_is_diagnosed_as_such(pdf):
    """Our fixture PDF has no text layer, so the pdftotext probe should name that reason."""
    respx.get(ALIVE).respond(200, text="true")
    respx.post(FULLTEXT).respond(500, text="empty content")
    client = _client()
    try:
        with pytest.raises(PdfUnparseableError) as exc:
            await client.fulltext_tei(pdf)
    finally:
        await client.aclose()
    # Either the specific text-layer reason, or an explicit "could not determine" — never a guess.
    assert "no text layer" in exc.value.reason or "could not be determined" in exc.value.reason


@respx.mock
async def test_empty_pdf_is_rejected_before_upload(tmp_path):
    respx.get(ALIVE).respond(200, text="true")
    upload = respx.post(FULLTEXT).respond(200, text=_TEI_OK)
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    client = _client()
    try:
        with pytest.raises(PdfUnparseableError, match="0 bytes"):
            await client.fulltext_tei(empty)
    finally:
        await client.aclose()
    assert upload.call_count == 0


@respx.mock
async def test_empty_200_body_is_an_error_not_an_empty_bibliography(pdf):
    """A blank success would otherwise become 'this paper has no references'."""
    respx.get(ALIVE).respond(200, text="true")
    respx.post(FULLTEXT).respond(200, text="   ")
    client = _client()
    try:
        with pytest.raises(GrobidRequestError, match="empty body"):
            await client.fulltext_tei(pdf)
    finally:
        await client.aclose()


@respx.mock
async def test_unexpected_4xx_is_reported_with_its_status(pdf):
    respx.get(ALIVE).respond(200, text="true")
    route = respx.post(FULLTEXT).respond(415, text="Unsupported Media Type")
    client = _client()
    try:
        with pytest.raises(GrobidRequestError, match="415"):
            await client.fulltext_tei(pdf)
    finally:
        await client.aclose()
    assert route.call_count == 1, "a 4xx is not transient"


def test_timeout_scales_with_document_size():
    assert _timeout_for(0) == 60.0                    # the base budget
    assert _timeout_for(2 * 1024 * 1024) == 80.0      # typical article: 60 + 10*2
    assert _timeout_for(20 * 1024 * 1024) == 260.0    # heavy report: 60 + 10*20
    assert _timeout_for(500 * 1024 * 1024) == 600.0   # clamped at the ceiling


def test_base_url_trailing_slash_is_normalized():
    assert GrobidClient("http://x:8070/").base_url == "http://x:8070"
