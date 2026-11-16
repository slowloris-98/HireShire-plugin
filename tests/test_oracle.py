"""Tests for the Oracle Recruiting Cloud board: the employer list, the scraper and
its rollout to existing installs. Fixtures are trimmed from live pods
(BDO on `ebqb.fa.us2.oraclecloud.com`, 2026-11-16)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import httpx
import pytest

from hireshire import config as config_mod
from hireshire import oracle_tenants, paths
from hireshire.rate_limit import RateLimiter
from hireshire.scrapers import oracle
from hireshire.scrapers.exceptions import SlugNotFoundError

HOST = "ebqb.fa.us2.oraclecloud.com"
ROW = {"token": "bdo-usa", "label": "BDO USA", "host": HOST,
       "sites": ["BDOExperiencedCareers", "CX_1"]}


def _req(i, date="2026-11-16", loc="New York, NY, United States"):
    return {"Id": str(i), "Title": f"Job {i}", "PostedDate": date, "PrimaryLocation": loc,
            "ShortDescriptionStr": ""}


def _page(reqs, total=None):
    return {"items": [{"TotalJobsCount": total if total is not None else len(reqs),
                       "requisitionList": reqs}]}


def _site_and_offset(request) -> tuple[str, int]:
    finder = unquote(request.url.params["finder"])
    fields = dict(part.split("=", 1) for part in finder.split(";", 1)[1].split(","))
    return fields["siteNumber"], int(fields["offset"])


def _scraper(handler, cutoff=None, tenants=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, oracle.OracleScraper(client, RateLimiter(4), retry_attempts=1, cutoff=cutoff,
                                        tenants={ROW["token"]: ROW} if tenants is None else tenants)


def _fetch(handler, cutoff=None, tenants=None, token="bdo-usa"):
    async def go():
        client, scraper = _scraper(handler, cutoff, tenants)
        async with client:
            return await scraper.fetch_all(token)
    return asyncio.run(go())


# --- The employer list ------------------------------------------------------------


def test_the_reader_cleans_rows_and_skips_what_it_cannot_use(tmp_path):
    path = tmp_path / oracle_tenants.FILENAME
    path.write_text(json.dumps([
        {**ROW, "token": "BDO-USA", "host": HOST.upper(), "sites": [" CX_1 ", ""]},
        {**ROW, "label": "duplicate"},
        {"token": "x", "label": "X", "host": "h", "sites": []},
        {"token": "y", "label": "Y", "sites": ["CX"]},
        "nonsense",
    ]), encoding="utf-8")
    assert oracle_tenants.entries(path) == [
        {"token": "bdo-usa", "label": "BDO USA", "host": HOST, "sites": ["CX_1"]}]


@pytest.mark.parametrize("content", [None, "not json", '{"a": 1}'])
def test_a_missing_or_unreadable_list_means_no_employers(tmp_path, content):
    path = tmp_path / oracle_tenants.FILENAME
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert oracle_tenants.entries(path) == []


def test_the_shipped_list_is_well_formed():
    raw = json.loads((paths.SHIPPED_CONFIG / oracle_tenants.FILENAME).read_text(encoding="utf-8"))
    assert len(oracle_tenants.entries()) == len(raw), "every shipped row must load"
    tokens = [r["token"] for r in raw]
    assert tokens == sorted(set(tokens))
    assert len({r["host"] for r in raw}) == len(raw)


# --- The scraper ------------------------------------------------------------------


def test_a_job_is_parsed_with_a_namespaced_id_and_a_public_link():
    scraped = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)
    job = oracle.parse_job("bdo-usa", HOST, "CX_1", _req(14715), scraped)
    assert job.source == "oracle" and job.board_token == "bdo-usa"
    assert job.job_id == "oracle:bdo-usa:14715"     # bare ids repeat between employers
    assert str(job.absolute_url) == f"https://{HOST}/hcmUI/CandidateExperience/en/sites/CX_1/job/14715"
    assert job.updated_at == datetime(2026, 11, 16, 23, 59, 59, tzinfo=timezone.utc)
    assert job.location.name == "New York, NY, United States"
    assert job.detail_path == f"{HOST}|CX_1|14715"
    assert job.content_text is None                  # list-only: the funnel hydrates


def test_a_late_posting_survives_the_scrapers_age_gate_on_the_next_days_sweep():
    """PostedDate has no time. A job posted at 22:00 UTC on the 15th is first seen by
    the 01:00 sweep on the 16th, whose 24-hour cutoff is 01:00 on the 15th. Read as
    midnight it would fall before that cutoff on this sweep and every later one."""
    sweep = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
    cutoff = sweep - timedelta(hours=24)
    job = oracle.parse_job("bdo-usa", HOST, "CX_1", _req(1, date="2026-11-15"), sweep)
    assert job.updated_at >= cutoff                   # the gate in scraper.py
    today = oracle.parse_job("bdo-usa", HOST, "CX_1", _req(2, date="2026-11-16"), sweep)
    assert today.updated_at == sweep                  # never later than the scrape


def test_every_site_is_walked_and_a_job_on_two_sites_is_kept_once():
    calls = []

    def handler(request):
        site, offset = _site_and_offset(request)
        calls.append((request.url.host, site, offset))
        reqs = [_req(1), _req(2)] if site == "BDOExperiencedCareers" else [_req(2), _req(3)]
        return httpx.Response(200, json=_page(reqs))

    jobs = _fetch(handler)
    assert [j.job_id for j in jobs] == ["oracle:bdo-usa:1", "oracle:bdo-usa:2", "oracle:bdo-usa:3"]
    assert calls == [(HOST, "BDOExperiencedCareers", 0), (HOST, "CX_1", 0)]


def test_the_walk_pages_by_200_and_stops_on_a_whole_page_before_the_cutoff_day():
    offsets = []

    def handler(request):
        site, offset = _site_and_offset(request)
        if site != "BDOExperiencedCareers":
            return httpx.Response(200, json=_page([]))
        offsets.append(offset)
        date = {0: "2026-11-16", 200: "2026-11-15", 400: "2026-11-14"}.get(offset, "2026-11-01")
        return httpx.Response(200, json=_page([_req(offset + i, date) for i in range(200)]))

    # Cutoff mid-day on the 15th: the 15th's page is still read (PostedDate has no
    # time), and the walk stops after the first page wholly before that day.
    jobs = _fetch(handler, cutoff=datetime(2026, 11, 15, 13, 0, tzinfo=timezone.utc))
    assert offsets == [0, 200, 400]
    assert len(jobs) == 600


def test_a_short_page_ends_the_site():
    offsets = []

    def handler(request):
        site, offset = _site_and_offset(request)
        offsets.append((site, offset))
        return httpx.Response(200, json=_page([_req(f"{site}{offset}")]))

    _fetch(handler)
    assert offsets == [("BDOExperiencedCareers", 0), ("CX_1", 0)]


def test_a_pod_that_answers_404_is_not_found():
    with pytest.raises(SlugNotFoundError):
        _fetch(lambda r: httpx.Response(404))


def test_an_unknown_token_asks_nothing_and_raises_nothing():
    calls = []
    assert _fetch(lambda r: calls.append(r) or httpx.Response(200), token="nope") == []
    assert calls == []


def test_a_page_that_is_not_list_json_ends_that_site_quietly():
    def handler(request):
        site, _ = _site_and_offset(request)
        if site == "CX_1":
            return httpx.Response(200, json=_page([_req(9)]))
        return httpx.Response(200, text="<html>maintenance</html>")

    assert [j.job_id for j in _fetch(handler)] == ["oracle:bdo-usa:9"]


DETAIL = {"items": [{
    "Id": "14715",
    "ExternalPostedStartDate": "2026-11-16T18:06:44+00:00",
    "ExternalDescriptionStr": "<p>" + "Advise clients on risk and controls. " * 15 + "</p>",
    "ExternalResponsibilitiesStr": "<ul><li>Lead fieldwork</li></ul>",
    "CorporateDescriptionStr": "<p>At BDO, how we show up matters.</p>",
    "secondaryLocations": [{"Name": "Chicago, IL, United States"},
                           {"Name": "New York, NY, United States"}],
}]}


def _hydrate(handler):
    job = oracle.parse_job("bdo-usa", HOST, "CX_1", _req(14715), datetime.now(timezone.utc))

    async def go():
        client, scraper = _scraper(handler, tenants={})
        async with client:
            return await scraper.fetch_detail(job)
    return asyncio.run(go())


def test_detail_fills_the_description_and_adds_the_secondary_locations():
    seen = []
    out = _hydrate(lambda r: seen.append(r) or httpx.Response(200, json=DETAIL))
    assert not out.detail_fetch_failed
    assert "risk and controls" in out.content_text and "Lead fieldwork" in out.content_text
    assert "how we show up" not in out.content_text   # the company boilerplate is left out
    assert out.location.name == "New York, NY, United States | Chicago, IL, United States"
    # The detail's exact posting time replaces the list's end-of-day reading.
    assert out.updated_at == datetime(2026, 11, 16, 18, 6, 44, tzinfo=timezone.utc)
    assert seen[0].url.host == HOST
    assert unquote(seen[0].url.params["finder"]) == 'ById;Id="14715",siteNumber=CX_1'


@pytest.mark.parametrize("response", [
    httpx.Response(500),
    httpx.Response(200, json={"items": [{"ExternalDescriptionStr": "<p>Short.</p>"}]}),
    httpx.Response(200, text="<html>"),
])
def test_a_failed_or_empty_detail_is_marked_not_raised(response):
    out = _hydrate(lambda r: response)
    assert out.detail_fetch_failed and out.content_text is None


def test_the_funnel_hydrates_oracle_jobs():
    from hireshire.funnel.detail_fetcher import DETAIL_SOURCES
    assert "oracle" in DETAIL_SOURCES


# --- Rollout to existing installs ---------------------------------------------------


@pytest.fixture
def shipped(tmp_path, monkeypatch):
    for board in ("greenhouse", "ashby", "lever", "gem", "direct", "workday", "bamboohr"):
        (tmp_path / f"{board}_companies.json").write_text(json.dumps([f"{board}-co"]), encoding="utf-8")
    (tmp_path / oracle_tenants.FILENAME).write_text(json.dumps([ROW]), encoding="utf-8")
    monkeypatch.setattr(paths, "SHIPPED_CONFIG", tmp_path)
    return tmp_path


def _load(tmp_path, platforms):
    yaml_path = tmp_path / "scraper.yaml"
    yaml_path.write_text(
        "settings:\n  enabled_platforms:\n" + "".join(f"    - {p}\n" for p in platforms),
        encoding="utf-8")
    return config_mod.load_config(yaml_path)


def test_an_install_on_the_previous_defaults_gains_oracle(shipped):
    cfg = _load(shipped, ["greenhouse", "ashby", "lever", "gem", "direct"])
    assert "oracle" in cfg.settings.enabled_platforms
    assert [(c.name, c.oracle_token) for c in cfg.oracle_companies] == [("BDO USA", "bdo-usa")]


def test_an_install_from_before_gem_gains_both(shipped):
    platforms = _load(shipped, ["greenhouse", "ashby", "lever", "direct"]).settings.enabled_platforms
    assert platforms[-2:] == ["gem", "oracle"]


def test_an_install_that_trimmed_the_defaults_keeps_its_choice(shipped):
    cfg = _load(shipped, ["greenhouse", "ashby", "lever", "direct", "workday"])
    cfg_trimmed = _load(shipped, ["greenhouse", "lever", "gem", "direct"])
    assert "oracle" in cfg.settings.enabled_platforms           # pre-Gem install: gets both
    assert "oracle" not in cfg_trimmed.settings.enabled_platforms
    assert cfg_trimmed.oracle_companies == []


def test_oracle_is_never_listed_twice(shipped):
    cfg = _load(shipped, ["greenhouse", "ashby", "lever", "gem", "direct", "oracle"])
    assert cfg.settings.enabled_platforms.count("oracle") == 1


def test_oracle_is_on_by_default_everywhere_it_is_declared():
    assert "oracle" in config_mod.ScraperSettings().enabled_platforms
    shipped_yaml = (paths.SHIPPED_CONFIG / "scraper.yaml").read_text(encoding="utf-8")
    assert "    - oracle\n" in shipped_yaml.replace("\r\n", "\n")
