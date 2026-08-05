"""GROBID client: PDF → TEI XML. The only module in this package that performs I/O.

GROBID is an external service this package does NOT manage. `AuditConfig.grobid_url` points at an
operator-supplied instance (a local container by default); if it is not answering, that is reported
with the command to start it — never worked around, and never allowed to degrade into an empty
reference list.

Modeled on `inspirations/sciwrite-lint/sciwrite_lint/pdf/grobid.py`, with three deliberate
divergences documented at their call sites: no `consolidateCitations`, no module-global client or
health cache, and no PDF-producer blacklist in the failure diagnosis.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import httpx
from loguru import logger

from reference_audit.sources.http import (
    DEFAULT_USER_AGENT,
    TransientHTTPError,
    is_transient_status,
    post_multipart,
)

GROBID_IMAGE = "docker.io/grobid/grobid:0.8.2.1-crf"
GROBID_START_HINT = (
    f"podman run -d --name grobid -p 8070:8070 {GROBID_IMAGE}\n"
    "  (first start takes ~30 s while the models load; then re-run this command)"
)

FULLTEXT_ENDPOINT = "/api/processFulltextDocument"
ALIVE_ENDPOINT = "/api/isalive"

# Pinned request parameters. These are part of the extraction contract, not tuning knobs: the
# committed TEI fixtures and the TEI mapper both depend on them, and `tests/pdf_fixtures.py` asserts
# it sends the same set.
#
# `consolidateCitations=0` is the load-bearing one. With `1` (which the sciwrite-lint client uses)
# GROBID looks each reference up in Crossref and REWRITES it with the matched canonical record. That
# would silently repair the exact defect this tool exists to detect — a garbled or hallucinated
# reference would arrive already corrected — and would smuggle a third-party network dependency into
# what is otherwise a call to a local service.
#
# `includeRawCitations=1` keeps `<note type="raw_reference">`, which is how a reference GROBID could
# not structure is still reported to a human instead of appearing as a blank record.
FULLTEXT_PARAMS = {
    "consolidateHeader": "0",
    "consolidateCitations": "0",
    "includeRawCitations": "1",
}

_ALIVE_TIMEOUT = 5.0
# GROBID throughput is roughly 1-3 MB/s and slower under concurrent load, so the timeout scales with
# document size rather than being a flat ceiling that a large report would always trip: a base budget
# plus a per-megabyte allowance, capped so we refuse to wait indefinitely.
_TIMEOUT_BASE_S = 60.0
_TIMEOUT_PER_MB_S = 10.0
_TIMEOUT_CEILING_S = 600.0

# Markers GROBID puts in a 500 body when it ran fine but could not parse THIS document.
_UNPARSEABLE_MARKERS = ("NO_BLOCKS", "empty content", "no text", "could not parse")


class GrobidError(RuntimeError):
    """The PDF could not be turned into a reference list. Always fatal for the run.

    There is no partial result to fall back to: without TEI there are no records to process
    independently, so this is the one place where a failure is whole-run rather than per-record.
    """


class GrobidUnavailableError(GrobidError):
    """The GROBID service is not answering at all."""


class GrobidRequestError(GrobidError):
    """GROBID answered, but with an error we could not get past (5xx/transport after retries)."""


class PdfUnparseableError(GrobidError):
    """GROBID is healthy but cannot parse *this* PDF (image-only, or unrecognizable structure).

    Kept distinct from `GrobidRequestError` so the message tells the user to fix the input rather
    than the service, and so it is never retried — the PDF will not become parseable.
    """

    def __init__(self, pdf_name: str, reason: str) -> None:
        super().__init__(f"cannot parse {pdf_name}: {reason}")
        self.pdf_name = pdf_name
        self.reason = reason


def _timeout_for(pdf_bytes: int) -> float:
    size_mb = pdf_bytes / (1024 * 1024)
    return min(_TIMEOUT_CEILING_S, _TIMEOUT_BASE_S + _TIMEOUT_PER_MB_S * size_mb)


def _is_unparseable_body(body: str) -> bool:
    lowered = body.lower()
    return any(marker.lower() in lowered for marker in _UNPARSEABLE_MARKERS)


def _diagnose_unparseable(pdf_path: Path) -> str:
    """Explain WHY a PDF is unparseable, or say plainly that we cannot tell.

    Only one probe: whether there is a text layer at all. `pdftotext` is a poppler tool GROBID itself
    depends on, but it is not a declared dependency here, so its absence is reported rather than
    silently skipped.

    The sciwrite-lint original also flags "known-problematic producers" (CorelDRAW, Preview, Quartz).
    That is deliberately not copied: a producer name correlates with, but does not establish, an
    unparseable structure, and reporting a guess as a diagnosis is exactly the incomplete-guess
    behavior this project forbids.
    """
    if not shutil.which("pdftotext"):
        return (
            "GROBID does not recognize this PDF's structure; the specific reason could not be "
            "determined (install poppler-utils' pdftotext for a text-layer diagnosis)"
        )
    try:
        text = subprocess.run(
            ["pdftotext", "-l", "3", str(pdf_path), "-"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("pdftotext diagnosis failed for {}: {}: {}", pdf_path.name, type(exc).__name__, exc)
        return (
            "GROBID does not recognize this PDF's structure; the text-layer probe itself failed "
            f"({type(exc).__name__})"
        )
    if len(text.strip()) < 100:
        return "image-only PDF (no text layer) — GROBID cannot OCR; run OCR over it first"
    return "GROBID does not recognize this PDF's structure, though it does contain extractable text"


class GrobidClient:
    """A thin client over one GROBID instance.

    Deliberately an injectable object with no module-level state, unlike the sciwrite-lint original:
    that client is shared by a long-lived batch process, where a cached health check pays for itself.
    An audit run parses exactly one PDF, so a TTL cache buys nothing and module globals would leak
    between tests and rebind across event loops.
    """

    def __init__(self, base_url: str, *, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(headers={"User-Agent": DEFAULT_USER_AGENT})
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    async def check_alive(self) -> None:
        """Raise `GrobidUnavailableError` unless the service answers. Called before the upload.

        Checked up front so an unreachable service fails in seconds with an actionable message,
        rather than after a multi-minute upload timeout.
        """
        url = f"{self.base_url}{ALIVE_ENDPOINT}"
        try:
            resp = await self._client.get(url, timeout=_ALIVE_TIMEOUT)
        except httpx.HTTPError as exc:
            raise GrobidUnavailableError(
                f"GROBID is not reachable at {self.base_url} ({type(exc).__name__}: {exc}).\n"
                f"PDF input needs a running GROBID; start one with:\n  {GROBID_START_HINT}\n"
                "Or point --grobid / GROBID_URL at an existing instance."
            ) from exc
        if resp.status_code != 200:
            raise GrobidUnavailableError(
                f"GROBID at {self.base_url} answered {ALIVE_ENDPOINT} with HTTP "
                f"{resp.status_code}, so it is not ready to accept documents.\n"
                f"If it is still loading models, wait ~30 s and retry; otherwise restart it:\n"
                f"  {GROBID_START_HINT}"
            )

    async def fulltext_tei(self, pdf_path: str | Path) -> str:
        """Upload a PDF and return its TEI XML.

        Raises `GrobidUnavailableError`, `PdfUnparseableError`, or `GrobidRequestError` — every
        failure mode is named, and none of them returns an empty string.
        """
        path = Path(pdf_path)
        await self.check_alive()

        pdf_bytes = path.read_bytes()
        if not pdf_bytes:
            raise PdfUnparseableError(path.name, "the file is empty (0 bytes)")

        url = f"{self.base_url}{FULLTEXT_ENDPOINT}"
        # A 500 that names a per-document parse failure is NOT transient: retrying it three times
        # only adds latency, and calling it an outage would blame the service for a bad input.
        def _transient(resp: httpx.Response) -> bool:
            return is_transient_status(resp) and not _is_unparseable_body(resp.text)

        try:
            resp = await post_multipart(
                self._client,
                url,
                files={"input": (path.name, pdf_bytes, "application/pdf")},
                data=dict(FULLTEXT_PARAMS),
                timeout=_timeout_for(len(pdf_bytes)),
                transient=_transient,
            )
        except TransientHTTPError as exc:
            raise GrobidRequestError(
                f"GROBID at {self.base_url} kept failing on {path.name} after 3 attempts ({exc}).\n"
                "Check the service logs:  podman logs grobid"
            ) from exc

        if resp.status_code == 200:
            if not resp.text.strip():
                raise GrobidRequestError(
                    f"GROBID returned an empty body (HTTP 200) for {path.name} — no TEI to parse."
                )
            return resp.text

        if _is_unparseable_body(resp.text):
            reason = _diagnose_unparseable(path)
            logger.info("GROBID cannot parse {}: {}", path.name, reason)
            raise PdfUnparseableError(path.name, reason)

        raise GrobidRequestError(
            f"GROBID at {self.base_url} returned HTTP {resp.status_code} for {path.name}: "
            f"{resp.text[:200]!r}\nCheck the service logs:  podman logs grobid"
        )
