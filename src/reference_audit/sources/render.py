"""Headless-browser rendering for client-side-rendered (single-page-app) web pages.

Some cited URLs are JavaScript single-page apps: a plain HTTP GET returns only an empty app shell
(a spinner plus a ``<script>`` bundle), and the page's real title and text exist only *after* the
JavaScript runs. Reading such a shell as 'a different page' would be a false hallucination
(see `matching.webcheck`). This module renders the page in a headless Chromium and returns the
post-JavaScript DOM so the page's own metadata/text can be read like any other page.

Reliability-first (per the project's two-level reporting goal):
  * if no browser is available, rendering is *unavailable* — the caller leaves the entry unresolved
    and reports that a browser is needed, never guesses 'different page' from the empty shell;
  * a render failure (timeout, crash, non-zero exit) is an *error* — reported, retried next run,
    never cached and never read as 'absent'.

The renderer shells out to a system Chromium/Chrome binary (``--headless=new --dump-dom``) rather
than pulling in a Python browser-automation package and its ~150 MB browser download: the only thing
needed is a chromium-family binary on PATH (or an explicit path via config), and everything degrades
gracefully to 'unavailable' when none is present.
"""

from __future__ import annotations

import asyncio
import shutil

# Candidate binary names searched on PATH, in preference order, when no explicit path is configured.
_BROWSER_NAMES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)


class RenderUnavailable(Exception):
    """No headless browser is available to render the page (a configuration gap, not a failure)."""


class RenderError(Exception):
    """A headless render was attempted but failed (timeout, crash, non-zero exit, empty output)."""


def find_browser(explicit_path: str | None = None) -> str | None:
    """Resolve a chromium-family binary: the configured path if runnable, else the first on PATH."""
    if explicit_path:
        return explicit_path if shutil.which(explicit_path) or _is_executable(explicit_path) else None
    for name in _BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _is_executable(path: str) -> bool:
    import os

    return os.path.isfile(path) and os.access(path, os.X_OK)


class ChromiumRenderer:
    """An injectable async renderer: ``await renderer(url) -> (status, final_url, html)``.

    Constructed once per run with a resolved browser path (or ``None`` — then every call raises
    `RenderUnavailable`, so the funnel reports 'a browser is needed' rather than crashing). Matches
    the ``fetch=`` stub contract of `WebAdapter`, so tests inject a plain callable instead.
    """

    def __init__(
        self,
        browser_path: str | None,
        *,
        timeout: float = 30.0,
        virtual_time_ms: int = 15000,
    ):
        self.browser_path = browser_path
        self.timeout = timeout
        self.virtual_time_ms = virtual_time_ms

    async def __call__(self, url: str) -> tuple[int, str, str]:
        if not self.browser_path:
            raise RenderUnavailable("no chromium-family browser found (set web_render_browser_path)")
        cmd = [
            self.browser_path,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--disable-dev-shm-usage",
            f"--virtual-time-budget={self.virtual_time_ms}",
            "--dump-dom",
            url,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:  # binary vanished / not executable
            raise RenderError(f"could not launch browser: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            proc.kill()
            await proc.wait()
            raise RenderError(f"render timed out after {self.timeout}s") from exc
        if proc.returncode != 0:
            tail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            raise RenderError(f"browser exited {proc.returncode}: {tail[0]}")
        html = (stdout or b"").decode("utf-8", "replace")
        if not html.strip():
            raise RenderError("browser produced empty output")
        # --dump-dom does not report redirects; the cited URL is the best 'final' URL we have.
        return 200, url, html
