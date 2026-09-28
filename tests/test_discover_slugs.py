"""Tests for the Common Crawl slug refresh, a maintenance tool the engine never runs.

Two rules are what these pin. The refresh is **add only**: with no skip list, a
404 is a deferral, so removing a shipped slug would be the verdict `scraper.py`
refuses to make. And a slug is admitted only when its board answers with at least
one posting, because every shipped slug is a request on every sweep for every user
— a junk token from the crawl would cost that forever.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_none

from hireshire import paths

sys.path.insert(0, str(paths.ROOT / "scripts"))

import discover_slugs as ds  # noqa: E402

GH, LEVER, ASHBY = ds.BOARDS["greenhouse"], ds.BOARDS["lever"], ds.BOARDS["ashby"]

# The real retry policy backs off for seconds; this one gives up at once.
FAST_RETRY = retry(
    retry=retry_if_exception(lambda e: isinstance(e, httpx.HTTPStatusError)),
    stop=stop_after_attempt(2), wait=wait_none(), reraise=True,
)


def _cdx(*urls: str) -> list[str]:
    return [json.dumps({"url": u}) for u in urls]


# --- Extraction -------------------------------------------------------------------


def test_the_slug_is_the_first_path_segment():
    lines = _cdx(
        "https://jobs.ashbyhq.com/wayve/1f0e-44",
        "https://jobs.ashbyhq.com/Wayve",
        "https://jobs.ashbyhq.com/clickhouse/",
        "https://jobs.ashbyhq.com/checkout.com/abc/application",
    )
    assert ds.slugs_from_cdx(ASHBY, lines) == {"wayve", "clickhouse", "checkout.com"}


def test_site_furniture_and_junk_are_not_slugs():
    lines = _cdx(
        "https://jobs.lever.co/robots.txt",
        "https://jobs.lever.co/favicon.ico",
        "https://jobs.lever.co/sitemap-3.xml",
        "https://jobs.lever.co/",
        "https://jobs.lever.co/_next/static/x.js",
        "https://jobs.lever.co/%20spaced%20out",
        "https://jobs.lever.co/zippi/abc",
    )
    assert ds.slugs_from_cdx(LEVER, lines) == {"zippi"}


def test_a_greenhouse_embed_names_its_board_in_for_and_never_in_token():
    # On embed/job_app, `token` is the JOB id; reading it would ship a number as a board.
    lines = _cdx(
        "https://boards.greenhouse.io/embed/job_board?for=flix&b=https://x",
        "https://boards.greenhouse.io/embed/job_app?for=monsterenergy&token=5123456",
        "https://boards.greenhouse.io/embed/job_app?token=987654",
    )
    assert ds.slugs_from_cdx(GH, lines) == {"flix", "monsterenergy"}


def test_the_eu_hosts_are_ignored_because_the_scraper_cannot_read_them():
    lines = _cdx(
        "https://job-boards.eu.greenhouse.io/someeu/jobs/1",
        "https://job-boards.greenhouse.io/someus/jobs/1",
    )
    assert ds.slugs_from_cdx(GH, lines) == {"someus"}
    assert ds.slug_from_url(LEVER, "https://jobs.eu.lever.co/euco") is None


def test_a_malformed_index_line_is_skipped_not_fatal():
    lines = ["<html>504</html>", "", json.dumps(["not", "a", "dict"])] + _cdx("jobs.ashbyhq.com/ok")
    assert ds.slugs_from_cdx(ASHBY, lines) == {"ok"}


# --- The live check ---------------------------------------------------------------


def _check(board, handler, slug="acme"):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ds.check(client, board, slug, FAST_RETRY)
    return asyncio.run(go())


def test_a_board_with_postings_is_admitted_on_every_response_shape():
    assert _check(GH, lambda r: httpx.Response(200, json={"jobs": [{}, {}], "meta": {}})) == ("admitted", 2)
    assert _check(ASHBY, lambda r: httpx.Response(200, json={"jobs": [{}]})) == ("admitted", 1)
    assert _check(LEVER, lambda r: httpx.Response(200, json=[{}])) == ("admitted", 1)


def test_a_live_board_with_no_postings_is_not_admitted():
    assert _check(GH, lambda r: httpx.Response(200, json={"jobs": []}))[0] == "empty"
    assert _check(LEVER, lambda r: httpx.Response(200, json=[]))[0] == "empty"


def test_a_404_is_not_found_and_a_persistent_503_is_unverified():
    assert _check(ASHBY, lambda r: httpx.Response(404))[0] == "not_found"
    assert _check(ASHBY, lambda r: httpx.Response(503))[0] == "unverified"
    assert _check(ASHBY, lambda r: httpx.Response(200, text="<html>"))[0] == "unverified"


def test_a_transient_503_is_retried_through():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json={"jobs": [{}]})

    assert _check(ASHBY, handler) == ("admitted", 1)
    assert len(calls) == 2


def test_the_check_hits_the_scrapers_own_endpoint():
    seen = []
    _check(LEVER, lambda r: seen.append(str(r.url)) or httpx.Response(200, json=[]), slug="zippi")
    assert seen[0].startswith(f"{ds.lever.BASE_URL}/zippi")


# --- Index pages ------------------------------------------------------------------


def test_a_failed_index_page_is_reported_and_the_rest_are_kept(tmp_path):
    def handler(request):
        params = request.url.params
        if params.get("showNumPages"):
            return httpx.Response(200, json={"pages": 2})
        if params["page"] == "0":
            return httpx.Response(200, text="\n".join(_cdx("jobs.ashbyhq.com/one")) + "\n")
        return httpx.Response(504)

    async def go():
        failed: list[str] = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            lines = await ds.cdx_lines(client, "CC-X", "jobs.ashbyhq.com/*", tmp_path,
                                       FAST_RETRY, failed, pause_s=0)
        return lines, failed

    lines, failed = asyncio.run(go())
    assert ds.slugs_from_cdx(ASHBY, lines) == {"one"}
    assert len(failed) == 1 and "page 1" in failed[0]
    # Only the page that answered is cached, so a rerun asks again for page 1.
    cached = sorted(p.name for p in tmp_path.iterdir())
    assert any(n.endswith("__0.jsonl") for n in cached)
    assert not any(n.endswith("__1.jsonl") for n in cached)


def test_a_page_cut_off_with_a_200_is_retried_and_never_cached(tmp_path):
    # Measured on the live index: a 200 whose body stops inside a URL. Caching it
    # would make every later run read the short page as the whole one.
    whole = "\n".join(_cdx("jobs.ashbyhq.com/one", "jobs.ashbyhq.com/two")) + "\n"
    cut = whole[: whole.index("two") + 1]
    bodies = {"0": [cut, whole], "1": [cut, cut, cut]}

    def handler(request):
        params = request.url.params
        if params.get("showNumPages"):
            return httpx.Response(200, json={"pages": 2})
        return httpx.Response(200, text=bodies[params["page"]].pop(0))

    async def go():
        failed: list[str] = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            lines = await ds.cdx_lines(client, "CC-X", "jobs.ashbyhq.com/*", tmp_path,
                                       FAST_RETRY, failed, pause_s=0, attempts=3)
        return lines, failed

    lines, failed = asyncio.run(go())
    assert ds.slugs_from_cdx(ASHBY, lines) == {"one", "two"}
    assert failed and "page 1" in failed[0] and "cut off" in failed[0]
    assert not any(p.name.endswith("__1.jsonl") for p in tmp_path.iterdir())


def test_page_completeness():
    assert ds.page_is_complete('{"url": "a"}\n{"url": "b"}\n')
    assert not ds.page_is_complete('{"url": "a"}\n{"url": "b')
    assert not ds.page_is_complete('{"url": "a"}\n{"url": "b"}')
    assert not ds.page_is_complete("")
    assert not ds.page_is_complete("<html>502</html>\n")


# --- Merge ------------------------------------------------------------------------


def test_merge_only_adds_and_keeps_the_file_format(tmp_path):
    path = tmp_path / "lever_companies.json"
    path.write_bytes(b'[\r\n  "kpmg",\r\n  "zeta"\r\n]')
    assert ds.merge(path, {"alpha", "zeta"}) == 1
    raw = path.read_bytes()
    assert json.loads(raw) == ["alpha", "kpmg", "zeta"]
    assert raw == b'[\r\n  "alpha",\r\n  "kpmg",\r\n  "zeta"\r\n]'


def test_merge_with_nothing_new_leaves_the_file_untouched(tmp_path):
    path = tmp_path / "x.json"
    path.write_bytes(b'[\n  "a",\n  "b"\n]\n')
    assert ds.merge(path, set()) == 0
    assert ds.merge(path, {"a"}) == 0
    assert path.read_bytes() == b'[\n  "a",\n  "b"\n]\n'


def test_nothing_in_the_tool_removes_a_slug():
    # The add-only rule is structural: the merge is a union, and no other code
    # path writes a list. A change that introduces removal must remove this test.
    source = (paths.ROOT / "scripts" / "discover_slugs.py").read_text(encoding="utf-8")
    assert source.count("write_bytes(") == 1
    assert "set(existing) | admitted" in source
    assert not re.search(r"\.(remove|discard)\(", source)


# --- The shipped lists ------------------------------------------------------------


def test_the_shipped_lists_are_sorted_unique_and_lowercase():
    for board in ds.BOARDS.values():
        slugs = json.loads((paths.SHIPPED_CONFIG / board.filename).read_text(encoding="utf-8"))
        assert slugs == sorted(set(slugs)), board.filename
        assert all(s == s.strip().lower() and s for s in slugs), board.filename
