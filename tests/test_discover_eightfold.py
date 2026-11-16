"""Tests for the Common Crawl Eightfold tenant refresh, a maintenance tool the engine never runs.

The rules are `discover_slugs.py`'s: add only, and a tenant is admitted only when
its own `/api/pcsx/search` answers with positions. What is new here is that a
tenant is a host plus a `domain=` value, so the extraction, the domain sources
and the row-shaped merge are what these pin. The real cases are from a live crawl
(`CC-MAIN-2026-43`) and live probes of the hosts it named.
"""
from __future__ import annotations

import asyncio
import collections
import json
import re
import sys

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_none

from hireshire import paths

sys.path.insert(0, str(paths.ROOT / "scripts"))

import discover_eightfold as de  # noqa: E402

FAST_RETRY = retry(
    retry=retry_if_exception(lambda e: isinstance(e, httpx.HTTPStatusError)),
    stop=stop_after_attempt(2), wait=wait_none(), reraise=True,
)


def _cdx(*urls: str) -> list[str]:
    return [json.dumps({"url": u}) for u in urls]


# --- Extraction -------------------------------------------------------------------


def test_a_tenant_is_the_subdomain_and_its_domain_comes_from_the_crawled_urls():
    found = de.candidates_from_cdx(_cdx(
        "https://bnymellon.eightfold.ai/careers/*/hong_kong?domain=bnymellon.com",
        "https://bnymellon.eightfold.ai/careers/accounting_services/x?domain=bnymellon.com",
        "https://BostonScientific.eightfold.ai/careers",
        "https://eightfold.ai/blog/post",           # Eightfold's own site, no subdomain
        "https://www.example.com/?domain=x.com",    # not Eightfold at all
        "https://a.b.eightfold.ai/careers",         # nested: not a tenant host
    ))
    assert set(found) == {"bnymellon.eightfold.ai", "bostonscientific.eightfold.ai"}
    assert found["bnymellon.eightfold.ai"] == collections.Counter({"bnymellon.com": 2})
    assert not found["bostonscientific.eightfold.ai"]


def test_a_malformed_index_line_is_skipped_not_fatal():
    lines = ["<html>504</html>", "", json.dumps(["x"])] + _cdx("https://ok.eightfold.ai/careers")
    assert set(de.candidates_from_cdx(lines)) == {"ok.eightfold.ai"}


def test_eightfolds_own_sites_and_customers_sandboxes_are_rejected():
    for token in ("app", "apidocs", "docs", "community", "cultivate", "cultivateeurope", "www",
                  "employee", "pages", "partners",
                  "aexp-sandbox", "citigroup-qa-sandbox", "acme-test", "acme-uat", "acme-dev1",
                  "acme-demo"):
        assert de.reject(token), token


def test_real_tenants_are_not_rejected():
    # From the live crawl, plus names a looser rule would catch.
    for token in ("bnymellon", "bostonscientific", "aexp", "coca-colafemsa", "10xgenomics",
                  "devon", "atestco", "qatarairways", "stagecoach"):
        assert not de.reject(token), token


def test_the_careers_page_yields_the_domain_and_label_from_its_escaped_config():
    page = ('<html><head><title>Careers at Boston Scientific</title></head><body>'
            '<code>{&#34;domain&#34;: &#34;bostonscientific.com&#34;, &#34;configs&#34;: {}}</code>')
    assert de.read_careers_page(page) == ("bostonscientific.com", "Boston Scientific")
    assert de.read_careers_page("<html><title>  </title></html>") == (None, None)


def test_the_token_is_the_subdomain_or_else_the_domains_first_label():
    assert de.token_for("bnymellon.eightfold.ai", None) == "bnymellon"
    assert de.token_for("careers.americanexpress.com", "aexp.com") == "aexp"
    assert de.token_for("careers.example.com", None) is None


# --- The live check ---------------------------------------------------------------


def test_eightfolds_placeholder_domain_is_never_a_tenants_domain():
    # kroger.eightfold.ai and employee.eightfold.ai report it: not configured there.
    page = '{&#34;domain&#34;: &#34;volkscience.com&#34;}'
    assert de.read_careers_page(page) == (None, None)
    found = de.candidates_from_cdx(_cdx("https://x.eightfold.ai/careers?domain=volkscience.com"))
    assert not found["x.eightfold.ai"]


def test_a_page_title_is_tidied_into_a_label():
    assert de._clean_label("Match Me Resume Match | Citi Careers") == "Citi"
    assert de._clean_label("Careers at Boston Scientific") == "Boston Scientific"
    assert de._clean_label("Infineon Careers") == "Infineon"
    assert de._clean_label("AstraZeneca") == "AstraZeneca"
    assert de._clean_label(" | ") is None


def _check(handler, host="acme.eightfold.ai", domains=None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     follow_redirects=True) as client:
            return await de.check(client, host, collections.Counter(domains or {}), FAST_RETRY)
    return asyncio.run(go())


def _routes(careers, search):
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return careers(request) if request.url.path == "/careers" else search(request)
    return handler, seen


def test_a_tenant_with_positions_is_admitted_on_the_scrapers_own_request():
    handler, seen = _routes(
        lambda r: httpx.Response(200, text="<title>Careers at Acme</title>"),
        lambda r: httpx.Response(200, json={"data": {"positions": [{}, {}], "count": 511}}))
    kind, row, n = _check(handler, domains={"acme.com": 3})
    assert (kind, n) == ("admitted", 511)
    assert row == {"token": "acme", "label": "Acme", "base": "https://acme.eightfold.ai",
                   "domain": "acme.com"}
    search = [u for u in seen if "/api/pcsx/search" in u][0]
    tenant = de.eightfold.Tenant("acme", "Acme", "https://acme.eightfold.ai", "acme.com")
    assert search == de.eightfold.list_url(tenant, None, 1)


def test_the_careers_page_supplies_the_domain_when_the_crawl_did_not():
    page = '<title>x</title>{&#34;domain&#34;: &#34;acme.io&#34;}'
    handler, seen = _routes(lambda r: httpx.Response(200, text=page),
                            lambda r: httpx.Response(200, json={"data": {"positions": [{}]}}))
    kind, row, _ = _check(handler)
    assert kind == "admitted" and row["domain"] == "acme.io"
    assert any("domain=acme.io" in u for u in seen)


def test_a_host_with_no_domain_anywhere_is_unverified_and_never_guessed():
    handler, seen = _routes(lambda r: httpx.Response(404),
                            lambda r: httpx.Response(200, json={"data": {"positions": [{}]}}))
    assert _check(handler)[0] == "unverified"
    assert not any("/api/pcsx/search" in u for u in seen)


def test_a_careers_404_costs_only_the_label():
    # BNY: the bare /careers page answers 404 while its crawled URLs carry the domain.
    handler, _ = _routes(lambda r: httpx.Response(404),
                         lambda r: httpx.Response(200, json={"data": {"positions": [{}]}}))
    kind, row, _ = _check(handler, domains={"acme.com": 1})
    assert kind == "admitted" and row["label"] == "acme"


def test_a_tenant_that_moved_is_asked_on_the_host_it_redirects_to():
    # netflix.eightfold.ai/careers -> explore.jobs.netflix.net/careers
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "acme.eightfold.ai":
            return httpx.Response(302, headers={"location": "https://jobs.acme.net/careers"})
        if request.url.path == "/careers":
            return httpx.Response(200, text="<title>Acme Careers</title>")
        return httpx.Response(200, json={"data": {"positions": [{}]}})

    kind, row, _ = _check(handler, domains={"acme.com": 1})
    assert kind == "admitted"
    assert row["base"] == "https://jobs.acme.net" and row["token"] == "acme"
    assert row["label"] == "Acme"
    assert any(u.startswith("https://jobs.acme.net/api/pcsx/search?") for u in seen)


def test_the_other_outcomes():
    page = lambda r: httpx.Response(200, text="")  # noqa: E731
    for search, expected in [
        (lambda r: httpx.Response(200, json={"data": {"positions": []}}), "empty"),
        (lambda r: httpx.Response(404, text="<html>"), "not_found"),   # moved off eightfold.ai
        (lambda r: httpx.Response(503), "unverified"),
        (lambda r: httpx.Response(200, text="<html>"), "unverified"),
    ]:
        handler, _ = _routes(page, search)
        assert _check(handler, domains={"acme.com": 1})[0] == expected


# --- Merge ------------------------------------------------------------------------

_ROW_A = {"token": "alpha", "label": "Alpha", "base": "https://alpha.eightfold.ai", "domain": "alpha.com"}
_ROW_Z = {"token": "zeta", "label": "Zeta", "base": "https://zeta.eightfold.ai", "domain": "zeta.com"}


def test_merge_only_adds_and_keeps_the_file_format(tmp_path):
    path = tmp_path / "eightfold_tenants.json"
    path.write_bytes(json.dumps([_ROW_Z], indent=2).replace("\n", "\r\n").encode() + b"\r\n")
    changed = {**_ROW_Z, "label": "Renamed"}
    assert de.merge(path, [_ROW_A, changed]) == 1
    raw = path.read_bytes()
    assert json.loads(raw) == [_ROW_A, _ROW_Z]       # sorted; the shipped row untouched
    assert b"\r\n" in raw and raw.endswith(b"\r\n") and b"Renamed" not in raw


def test_merge_with_nothing_new_leaves_the_file_untouched(tmp_path):
    path = tmp_path / "eightfold_tenants.json"
    path.write_bytes(b"[]\n")
    assert de.merge(path, []) == 0
    assert path.read_bytes() == b"[]\n"


def test_a_token_that_is_another_boards_slug_is_suffixed():
    # Lever ships `deloitte`; an unsuffixed tenant would switch off auto-apply for it.
    assert de.unclash("deloitte", {"deloitte", "x"}) == "deloitte-eightfold"
    assert de.unclash("citi", {"deloitte"}) == "citi"


def test_no_shipped_tenant_token_is_another_boards_slug():
    """The applier excludes by token. A slug batch that adds a board named like a
    shipped tenant must rename the tenant (or not ship the slug) first."""
    shipped = {r["token"] for r in json.loads(
        (paths.SHIPPED_CONFIG / de.tenants.FILENAME).read_text(encoding="utf-8"))}
    assert not shipped & de.other_board_slugs()


def test_nothing_in_the_tool_removes_a_tenant():
    # The add-only rule is structural, as in discover_slugs.py: one write, of the
    # existing rows plus fresh ones, and no removal anywhere.
    source = (paths.ROOT / "scripts" / "discover_eightfold.py").read_text(encoding="utf-8")
    assert source.count("write_bytes(") == 1
    assert "[*existing, *fresh]" in source
    assert not re.search(r"\.(remove|discard|pop)\(", source)
