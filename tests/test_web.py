"""Web-artifact verification: HTML-metadata extraction, the fetch adapter, the funnel, and the
pipeline integration over a URL-only @misc (mordvintsev2022particle, the development oracle case).

The page fetch is injected (no network); the LLM is the in-memory FakeLLM. The reliability contract
is asserted at each stage: a transport error / dead link never becomes a false 'no match'.
"""

from __future__ import annotations

import pytest

from reference_audit.cache.store import AuditCache
from reference_audit.config import AuditConfig
from reference_audit.llm.client import LLMError
from reference_audit.matching.webcheck import check_web_reference
from reference_audit.models import BibEntry, EntryType, Identifiers, WebMatchResult
from reference_audit.pipeline import AuditPipeline
from reference_audit.sources.base import SourceAdapter
from reference_audit.sources.http import TransientHTTPError
from reference_audit.models import SourceQueryResult
from reference_audit.sources.render import RenderError, RenderUnavailable
from reference_audit.sources.web import WebAdapter, _looks_unrendered, extract_web_metadata

CFG = AuditConfig(model="t")

# A client-side-rendered single-page-app shell (Angular): a spinner + a <script type="module">
# bundle, real content injected only after JS. Modeled on the real data.europa.eu dataset page.
_SPA_SHELL = (
    '<!DOCTYPE html><html lang="en"><head>'
    "<title>European Data Portal</title>"
    '<script type="module" crossorigin src="/data/app.js"></script>'
    "</head><body><div id=\"app\"><div class=\"spinner\"> </div></div></body></html>"
)
# What the same URL looks like after a headless browser runs the JavaScript.
_SPA_RENDERED = (
    "<html><head><title>Special Eurobarometer SP523 : Corruption</title>"
    '<meta property="og:title" content="Special Eurobarometer SP523 : Corruption">'
    "</head><body><h1>Special Eurobarometer SP523 : Corruption</h1>"
    "<p>Corruption remains a serious concern for EU citizens.</p></body></html>"
)

_URL = "https://google-research.github.io/self-organising-systems/particle-lenia/"

# A trimmed copy of the real Particle Lenia page head (Open Graph + article:author + <title>).
_LENIA_HTML = """<!doctype html><html><head>
<meta charset="utf-8">
<title>Particle Lenia and the energy-based formulation</title>
<meta name="description" content="Simple particle-based artificial life-form"/>
<meta property="og:title" content="Particle Lenia and the energy-based formulation">
<meta property="og:site_name" content="Self-Organising Systems">
<meta property="article:author" content="Alexander Mordvintsev">
<meta property="article:author" content="Eyvind Niklasson">
<meta property="article:author" content="Ettore Randazzo">
<meta name="citation_publication_date" content="2022/12/23">
</head><body>
<h1>Particle Lenia and the energy-based formulation</h1>
<script>var ignore = 1;</script>
<p>Particle Lenia is a particle-based artificial life model.</p>
</body></html>"""


def _entry(title="Particle Lenia and the energy-based formulation",
           authors=("Mordvintsev, Alexander", "Niklasson, Eyvind", "Randazzo, Ettore")):
    return BibEntry(
        key="mordvintsev2022particle",
        entry_type=EntryType.MISC,
        title=title,
        authors=list(authors),
        year=2022,
        ids=Identifiers(url=_URL),
    )


def _stub_fetch(status=200, final_url=_URL, html=_LENIA_HTML, exc=None):
    async def fetch(_url):
        if exc is not None:
            raise exc
        return status, final_url, html
    return fetch


def _stub_render(html=_SPA_RENDERED, final_url=_URL, exc=None, calls=None):
    async def render(_url):
        if calls is not None:
            calls.append(_url)
        if exc is not None:
            raise exc
        return 200, final_url, html
    return render


class FakeLLM:
    """Programmable stand-in for LLMClient. `decider(user)` → WebMatchResult | 'raise'."""

    def __init__(self, decider):
        self.decider = decider
        self.calls = 0

    async def structured(self, system, user, schema_model, schema_name):
        self.calls += 1
        out = self.decider(user)
        if out == "raise":
            raise LLMError("boom")
        return out

    async def aclose(self):
        pass


# ── metadata extraction (no network) ─────────────────────────────────────────


def test_extract_metadata_og_and_authors():
    rec = extract_web_metadata(_LENIA_HTML, _URL)
    assert rec.source == "web"
    assert rec.title == "Particle Lenia and the energy-based formulation"
    assert rec.authors == ["Alexander Mordvintsev", "Eyvind Niklasson", "Ettore Randazzo"]
    assert rec.year == 2022
    assert rec.raw["site_name"] == "Self-Organising Systems"
    assert rec.raw["description"] == "Simple particle-based artificial life-form"
    assert "particle-based artificial life model" in rec.raw["text"]
    assert "var ignore" not in rec.raw["text"]  # <script> stripped before text extraction
    assert rec.ids.url == _URL


def test_extract_metadata_title_fallback_no_meta():
    rec = extract_web_metadata("<html><head><title>Just A Title</title></head><body>x</body></html>", _URL)
    assert rec.title == "Just A Title"
    assert rec.authors == []


def test_extract_metadata_empty_page():
    rec = extract_web_metadata("<html><body></body></html>", _URL)
    assert rec.title == ""
    assert rec.authors == []
    assert rec.raw["dead"] is False


# ── fetch adapter (injected fetcher) ─────────────────────────────────────────


async def test_adapter_live_page_extracts_record():
    res = await WebAdapter(fetch=_stub_fetch()).fetch_page(_URL)
    assert res.query_kind == "web" and res.error is None
    assert len(res.records) == 1
    assert res.records[0].title == "Particle Lenia and the energy-based formulation"


async def test_adapter_dead_link_is_record_not_error():
    res = await WebAdapter(fetch=_stub_fetch(status=404, html="")).fetch_page(_URL)
    assert res.error is None                      # a 404 is a finding, not an outage
    assert res.records[0].raw["dead"] is True
    assert res.records[0].raw["status"] == 404


async def test_adapter_transport_error_is_reported_not_absent():
    res = await WebAdapter(fetch=_stub_fetch(exc=TransientHTTPError("http 403"))).fetch_page(_URL)
    assert res.records == []
    assert res.error is not None and "could not fetch" in res.error


async def test_adapter_empty_url_is_empty_not_error():
    res = await WebAdapter().fetch_page("")
    assert res.records == [] and res.error is None


# ── SPA (client-side-rendered) detection + headless render ────────────────────


def test_looks_unrendered_detects_spa_shell_but_not_content_page():
    assert _looks_unrendered(_SPA_SHELL, extract_web_metadata(_SPA_SHELL, _URL)) is True
    # A real content page (the Lenia page) has plenty of text → never treated as a shell.
    assert _looks_unrendered(_LENIA_HTML, extract_web_metadata(_LENIA_HTML, _URL)) is False
    # A short page with NO SPA markers is not a shell either (no render to attempt).
    plain = "<html><body>hello world</body></html>"
    assert _looks_unrendered(plain, extract_web_metadata(plain, _URL)) is False


async def test_adapter_renders_spa_shell_then_extracts():
    calls = []
    res = await WebAdapter(
        fetch=_stub_fetch(html=_SPA_SHELL), render=_stub_render(calls=calls)
    ).fetch_page(_URL)
    rec = res.records[0]
    assert calls == [_URL]                                   # the shell triggered a render
    assert rec.raw["render"] == "rendered"
    assert rec.title == "Special Eurobarometer SP523 : Corruption"


async def test_adapter_non_shell_does_not_render():
    calls = []
    res = await WebAdapter(fetch=_stub_fetch(), render=_stub_render(calls=calls)).fetch_page(_URL)
    assert calls == []                                       # a content page is never rendered
    assert res.records[0].raw["render"] == "not_needed"


async def test_adapter_spa_shell_no_renderer_is_unavailable():
    res = await WebAdapter(fetch=_stub_fetch(html=_SPA_SHELL), render=None).fetch_page(_URL)
    assert res.error is None
    assert res.records[0].raw["render"] == "unavailable"     # never a fabricated page, just a gap


async def test_adapter_spa_shell_render_unavailable_marker():
    res = await WebAdapter(
        fetch=_stub_fetch(html=_SPA_SHELL), render=_stub_render(exc=RenderUnavailable("no browser"))
    ).fetch_page(_URL)
    assert res.error is None
    assert res.records[0].raw["render"] == "unavailable"


async def test_adapter_spa_shell_render_error_is_reported_not_absent():
    res = await WebAdapter(
        fetch=_stub_fetch(html=_SPA_SHELL), render=_stub_render(exc=RenderError("timeout"))
    ).fetch_page(_URL)
    assert res.records == []                                 # a render failure is an outage…
    assert res.error is not None and "rendering failed" in res.error  # …reported, retried, uncached


async def test_adapter_spa_shell_still_empty_after_render():
    res = await WebAdapter(
        fetch=_stub_fetch(html=_SPA_SHELL), render=_stub_render(html=_SPA_SHELL)
    ).fetch_page(_URL)
    assert res.records[0].raw["render"] == "rendered_empty"


# ── the funnel ───────────────────────────────────────────────────────────────


async def _check(entry, fetch_result, llm):
    return await check_web_reference(entry, fetch_result, llm, CFG, None)


async def test_funnel_metadata_confirms_without_llm():
    res = await WebAdapter(fetch=_stub_fetch()).fetch_page(_URL)
    verdict, issues = await _check(_entry(), res, None)   # llm=None: metadata alone must confirm
    assert verdict.kind == "exactly_one"
    assert verdict.artifacts[0].best_record.source == "web"
    # A clean confirm carries its rationale on the verdict, NOT as a needs-attention issue.
    assert "HTML metadata matches" in verdict.rationale
    assert issues == []


async def test_funnel_metadata_mismatch_then_llm_affirms():
    # No metadata title (so step 2 cannot confirm) → LLM affirms from the page text.
    res = await WebAdapter(fetch=_stub_fetch(html="<html><body>Particle Lenia demo</body></html>")).fetch_page(_URL)
    llm = FakeLLM(lambda u: WebMatchResult(corresponds=True, confidence="high", reason="same topic"))
    verdict, issues = await _check(_entry(), res, llm)
    assert verdict.kind == "exactly_one"
    assert llm.calls == 1
    assert "LLM-verified" in verdict.rationale
    assert issues == []


async def test_funnel_llm_high_confidence_reject_is_none():
    res = await WebAdapter(fetch=_stub_fetch(html="<html><title>Some other site</title><body>login</body></html>")).fetch_page(_URL)
    llm = FakeLLM(lambda u: WebMatchResult(corresponds=False, confidence="high", reason="login page"))
    verdict, issues = await _check(_entry(), res, llm)
    assert verdict.kind == "none"
    assert any("does NOT correspond" in i for i in issues)


async def test_funnel_llm_low_confidence_stays_unresolved():
    res = await WebAdapter(fetch=_stub_fetch(html="<html><body>ambiguous</body></html>")).fetch_page(_URL)
    llm = FakeLLM(lambda u: WebMatchResult(corresponds=False, confidence="low", reason="too little content"))
    verdict, issues = await _check(_entry(), res, llm)
    assert verdict is None
    assert any("could not be confirmed" in i for i in issues)


async def test_funnel_no_llm_unresolved_on_metadata_miss():
    res = await WebAdapter(fetch=_stub_fetch(html="<html><body>no metadata here</body></html>")).fetch_page(_URL)
    verdict, issues = await _check(_entry(), res, None)
    assert verdict is None
    assert any("LLM check is disabled" in i for i in issues)


async def test_funnel_spa_unavailable_is_unresolved_never_none():
    # Regression: an un-renderable SPA shell must NEVER become a `none` (false hallucination).
    res = await WebAdapter(fetch=_stub_fetch(html=_SPA_SHELL), render=None).fetch_page(_URL)
    # A FakeLLM that would shout "different page" if ever reached — it must NOT be reached.
    llm = FakeLLM(lambda u: WebMatchResult(corresponds=False, confidence="high", reason="landing page"))
    verdict, issues = await _check(_entry(), res, llm)
    assert verdict is None
    assert llm.calls == 0
    assert any("no headless browser is available" in i for i in issues)


async def test_funnel_spa_rendered_empty_is_unresolved():
    res = await WebAdapter(
        fetch=_stub_fetch(html=_SPA_SHELL), render=_stub_render(html=_SPA_SHELL)
    ).fetch_page(_URL)
    verdict, issues = await _check(_entry(), res, FakeLLM(lambda u: "raise"))
    assert verdict is None
    assert any("stayed empty even after headless rendering" in i for i in issues)


async def test_funnel_spa_rendered_confirms_via_metadata():
    # The data.europa.eu case: a shell that, once rendered, matches the cited title → exactly_one.
    res = await WebAdapter(
        fetch=_stub_fetch(html=_SPA_SHELL), render=_stub_render()
    ).fetch_page(_URL)
    entry = _entry(title="Special Eurobarometer 523: Corruption", authors=())
    verdict, issues = await _check(entry, res, None)         # confirmed without any LLM call
    assert verdict.kind == "exactly_one"
    assert "HTML metadata matches" in verdict.rationale
    assert issues == []


async def test_funnel_dead_link_unresolved_with_flag():
    res = await WebAdapter(fetch=_stub_fetch(status=404, html="")).fetch_page(_URL)
    verdict, issues = await _check(_entry(), res, FakeLLM(lambda u: "raise"))
    assert verdict is None
    assert any("dead link" in i for i in issues)


async def test_funnel_fetch_error_unresolved():
    res = await WebAdapter(fetch=_stub_fetch(exc=TransientHTTPError("timeout"))).fetch_page(_URL)
    verdict, issues = await _check(_entry(), res, None)
    assert verdict is None
    assert any("retry next run" in i for i in issues)


async def test_funnel_llm_error_unresolved():
    res = await WebAdapter(fetch=_stub_fetch(html="<html><body>x</body></html>")).fetch_page(_URL)
    verdict, issues = await _check(_entry(), res, FakeLLM(lambda u: "raise"))
    assert verdict is None
    assert any("LLM check failed" in i for i in issues)


# ── pipeline integration ─────────────────────────────────────────────────────

_BIB = (
    "@misc{mordvintsev2022particle,\n"
    "  title = {Particle {L}enia and the energy-based formulation},\n"
    "  author = {Mordvintsev, Alexander and Niklasson, Eyvind and Randazzo, Ettore},\n"
    "  year = {2022},\n"
    f"  url = {{{_URL}}}\n"
    "}\n"
)


def _write_bib(tmp_path):
    bib = tmp_path / "r.bib"
    bib.write_text(_BIB, encoding="utf-8")
    return bib


async def test_pipeline_web_only_confirms_via_metadata(tmp_path):
    pipe = AuditPipeline(CFG, adapters=[WebAdapter(fetch=_stub_fetch())], llm=None)
    report = await pipe.run(None, _write_bib(tmp_path))
    await pipe.aclose()
    audit = report.entries[0]
    assert audit.verdict.kind == "exactly_one"
    assert "confirmed via web page" in audit.verdict.rationale
    # A clean confirm is not flagged: no ⚠ issues at all (it lands in the report's NO-ISSUES group).
    assert audit.issues == []


async def test_pipeline_web_confirm_renders_under_no_issues(tmp_path):
    # Regression: a confirmed web page must NOT be flagged as an issue (⚠) — the success belongs on
    # the verdict line, and the entry belongs in the NO-ISSUES group.
    from reference_audit.report import render_text

    pipe = AuditPipeline(CFG, adapters=[WebAdapter(fetch=_stub_fetch())], llm=None)
    report = await pipe.run(None, _write_bib(tmp_path))
    await pipe.aclose()
    text = render_text(report)
    assert "NO ISSUES (1)" in text
    assert "— problems, possible hallucinations" not in text  # no needs-attention group at all
    assert "⚠" not in text                                    # the confirmation is never a ⚠ line
    assert "matched: url:" in text                            # the matched URL is shown on the verdict line


async def test_pipeline_web_dead_link_left_unresolved(tmp_path):
    pipe = AuditPipeline(CFG, adapters=[WebAdapter(fetch=_stub_fetch(status=404, html=""))], llm=None)
    report = await pipe.run(None, _write_bib(tmp_path))
    await pipe.aclose()
    audit = report.entries[0]
    assert audit.verdict is None                  # never a false 'no match'
    assert any("dead link" in i for i in audit.issues)


async def test_pipeline_web_cached_run_makes_no_refetch(tmp_path):
    cache = AuditCache(tmp_path / "c.db", pipeline_version=CFG.pipeline_version, model="t")
    fetch = _stub_fetch()
    calls = {"n": 0}

    async def counting_fetch(url):
        calls["n"] += 1
        return await fetch(url)

    bib = _write_bib(tmp_path)
    pipe1 = AuditPipeline(CFG, cache=cache, adapters=[WebAdapter(fetch=counting_fetch)], llm=None)
    r1 = await pipe1.run(None, bib)
    await pipe1.aclose()
    assert r1.entries[0].verdict.kind == "exactly_one"
    assert calls["n"] == 1

    # Second run: verdict from cache, fetch served from the source-query cache → no new fetch, and
    # the confirm issue is re-derived identically.
    pipe2 = AuditPipeline(CFG, cache=cache, adapters=[WebAdapter(fetch=counting_fetch)], llm=None)
    r2 = await pipe2.run(None, bib)
    await pipe2.aclose()
    assert r2.entries[0].from_cache is True
    assert r2.entries[0].verdict.kind == "exactly_one"
    assert "confirmed via web page" in r2.entries[0].verdict.rationale
    assert r2.entries[0].issues == []
    assert calls["n"] == 1
    cache.close()


class _EmptyScholarly(SourceAdapter):
    """A scholarly adapter that finds nothing — so a URL-only @misc takes the *main* pipeline path
    (non-empty route → empty candidates → web fallback), not the reduced-adapter early return."""

    name = "crossref"  # @misc routes by-metadata to crossref, so this makes the route non-empty

    async def search_by_metadata(self, entry, limit=10):
        return SourceQueryResult(source=self.name, query_kind="metadata", records=[])


async def test_pipeline_web_main_path_when_scholarly_empty(tmp_path):
    pipe = AuditPipeline(
        CFG, adapters=[_EmptyScholarly(), WebAdapter(fetch=_stub_fetch())], llm=None
    )
    report = await pipe.run(None, _write_bib(tmp_path))
    await pipe.aclose()
    audit = report.entries[0]
    assert audit.verdict.kind == "exactly_one"
    assert "confirmed via web page" in audit.verdict.rationale


@pytest.mark.parametrize("status", [404, 410])
async def test_pipeline_web_gone_statuses(tmp_path, status):
    pipe = AuditPipeline(CFG, adapters=[WebAdapter(fetch=_stub_fetch(status=status, html=""))], llm=None)
    report = await pipe.run(None, _write_bib(tmp_path))
    await pipe.aclose()
    assert report.entries[0].verdict is None


def test_web_fetch_predates_render_detects_old_cache():
    from reference_audit.models import SourceRecord
    from reference_audit.pipeline import _web_fetch_predates_render

    def result(*recs):
        return SourceQueryResult(source="web", query_kind="web", records=list(recs))

    pre = SourceRecord(source="web", source_native_id=_URL, raw={"status": 200, "dead": False})
    post = SourceRecord(source="web", source_native_id=_URL, raw={"status": 200, "render": "rendered"})
    dead = SourceRecord(source="web", source_native_id=_URL, raw={"status": 404, "dead": True})
    assert _web_fetch_predates_render(result(pre)) is True    # no render marker → re-fetch
    assert _web_fetch_predates_render(result(post)) is False  # already rendered-aware
    assert _web_fetch_predates_render(result(dead)) is False  # a dead link is still valid


async def test_pipeline_refetches_stale_web_cache_to_render(tmp_path):
    # A web fetch cached BEFORE rendering existed (no `render` marker) must be re-fetched so the SPA
    # render path runs — the source-query cache is not versioned by pipeline_version.
    cache = AuditCache(tmp_path / "c.db", pipeline_version=CFG.pipeline_version, model="t")
    from reference_audit.parsing.bib import parse_bib

    entry = parse_bib(_write_bib(tmp_path))[0][0]
    stale = SourceQueryResult(
        source="web",
        query_kind="web",
        records=[extract_web_metadata(_SPA_SHELL, _URL)],  # an un-rendered shell, no render marker
    )
    cache.put_source_query(entry.content_hash, stale)

    calls = []
    # The stub renders the shell into the real Lenia content page, which matches the cited entry.
    web = WebAdapter(fetch=_stub_fetch(html=_SPA_SHELL), render=_stub_render(html=_LENIA_HTML, calls=calls))
    pipe = AuditPipeline(CFG, cache=cache, adapters=[web], llm=None)
    report = await pipe.run(None, _write_bib(tmp_path))
    await pipe.aclose()
    cache.close()
    assert calls == [_URL]                                    # stale shell forced a re-fetch+render
    assert report.entries[0].verdict.kind == "exactly_one"
