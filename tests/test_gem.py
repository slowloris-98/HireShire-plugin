"""Gem job boards: the scraper's parsing, and how existing installs come to sweep it.

The fixture is trimmed from a real `api.gem.com/job_board/v0/cip/job_posts/` response.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from hireshire import config as config_mod
from hireshire import paths
from hireshire.rate_limit import RateLimiter
from hireshire.scrapers import gem
from hireshire.scrapers.exceptions import SlugNotFoundError

POSTING = {
    "absolute_url": "https://jobs.gem.com/cip/am9icG9zdDqhkGa9b2kAyABX5vKQFuuJ",
    "content": "<h3>About Collective Intelligence Project</h3><div>We build &amp; ship.</div>",
    "content_plain": "About Collective Intelligence Project\nWe build & ship.",
    "created_at": "2026-10-22T17:50:12.690Z",
    "departments": [{"child_ids": [], "id": "ZGVwdDqjunGnCWc3WS9IW2MGhubA",
                     "name": "Product & Engineering", "parent_id": None}],
    "employment_type": "full_time",
    "first_published_at": "2026-10-22T19:47:36.534Z",
    "id": "am9icG9zdDqhkGa9b2kAyABX5vKQFuuJ",
    "internal_job_id": "am9iOjKEpfPWp2iJQ7J_xPjT0Ag",
    "location": {"name": "United States - Remote"},
    "location_type": "remote",
    "offices": [
        {"id": "bG9jOo0Xz0ye2-mGNw57ybTX4xk", "location": {"name": "United States - Remote"},
         "name": "Los Angeles", "parent_id": None},
        {"id": "bG9jOoPfgRQieosmHj1H9xfzNNk", "location": {"name": "United States - Remote"},
         "name": "San Francisco", "parent_id": None},
    ],
    "requisition_id": "R1",
    "title": "Software Engineer, Product",
    "updated_at": "2026-10-26T22:02:50.442Z",
}

FAST_RETRY_ATTEMPTS = 1


def _fetch(handler, slug="cip"):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scraper = gem.GemScraper(client, RateLimiter(4, 0.0), FAST_RETRY_ATTEMPTS)
            return await scraper.fetch_all(slug)
    return asyncio.run(go())


# --- The scraper --------------------------------------------------------------------


def test_a_posting_parses_with_its_string_ids_numbered_by_position():
    seen = []
    jobs = _fetch(lambda r: seen.append(str(r.url)) or httpx.Response(200, json=[POSTING]))
    assert seen == ["https://api.gem.com/job_board/v0/cip/job_posts/"]
    [job] = jobs
    assert job.source == "gem" and job.board_token == "cip"
    assert job.job_id == POSTING["id"]
    assert job.title == "Software Engineer, Product"
    assert job.location.name == "United States - Remote"
    assert [(o.id, o.name, o.location) for o in job.offices] == [
        (0, "Los Angeles", "United States - Remote"),
        (1, "San Francisco", "United States - Remote"),
    ]
    assert [d.name for d in job.departments] == ["Product & Engineering"]
    assert job.requisition_id == "R1"
    assert str(job.absolute_url) == POSTING["absolute_url"]


def test_the_description_is_real_html_and_stripped_once():
    [job] = _fetch(lambda r: httpx.Response(200, json=[POSTING]))
    assert job.content_text == "About Collective Intelligence Project We build & ship."


def test_the_age_is_the_publication_date_not_the_last_edit():
    # An edit bumps `updated_at`; reading it would re-admit an old posting as new.
    [job] = _fetch(lambda r: httpx.Response(200, json=[POSTING]))
    assert job.updated_at == datetime(2026, 10, 22, 19, 47, 36, 534000, tzinfo=timezone.utc)


def test_a_missing_publication_date_falls_back_rather_than_dropping_the_job():
    entry = {k: v for k, v in POSTING.items() if k not in ("first_published_at", "created_at")}
    [job] = _fetch(lambda r: httpx.Response(200, json=[entry]))
    assert job.updated_at.day == 26


def test_a_malformed_posting_is_skipped_and_the_rest_kept():
    broken = {k: v for k, v in POSTING.items() if k != "title"}
    jobs = _fetch(lambda r: httpx.Response(200, json=[broken, POSTING, "junk"]))
    assert [j.title for j in jobs] == ["Software Engineer, Product"]


def test_an_empty_board_is_no_jobs():
    assert _fetch(lambda r: httpx.Response(200, json=[])) == []


def test_an_unknown_slug_is_slug_not_found():
    body = {"code": 404, "message": "Job board with vanity URL path nope not found"}
    with pytest.raises(SlugNotFoundError):
        _fetch(lambda r: httpx.Response(404, json=body), slug="nope")


# --- Rollout to existing installs ---------------------------------------------------


@pytest.fixture
def shipped(tmp_path, monkeypatch):
    for board in ("greenhouse", "ashby", "lever", "gem", "direct", "workday", "bamboohr"):
        (tmp_path / f"{board}_companies.json").write_text(json.dumps([f"{board}-co"]), encoding="utf-8")
    monkeypatch.setattr(paths, "SHIPPED_CONFIG", tmp_path)
    return tmp_path


def _load(tmp_path, platforms):
    yaml_path = tmp_path / "scraper.yaml"
    yaml_path.write_text(
        "settings:\n  enabled_platforms:\n" + "".join(f"    - {p}\n" for p in platforms),
        encoding="utf-8",
    )
    return config_mod.load_config(yaml_path)


def test_an_install_on_the_old_defaults_gains_gem(shipped):
    # The user's scraper.yaml lives in DATA and never receives a new shipped default.
    cfg = _load(shipped, ["greenhouse", "ashby", "lever", "direct"])
    assert "gem" in cfg.settings.enabled_platforms
    assert [c.gem_token for c in cfg.gem_companies] == ["gem-co"]


def test_an_install_that_also_enabled_the_slow_boards_gains_gem(shipped):
    cfg = _load(shipped, ["greenhouse", "ashby", "lever", "direct", "workday", "bamboohr"])
    assert cfg.gem_companies


def test_an_install_that_trimmed_the_defaults_keeps_its_choice(shipped):
    cfg = _load(shipped, ["greenhouse", "ashby", "direct"])
    assert "gem" not in cfg.settings.enabled_platforms
    assert cfg.gem_companies == []


def test_gem_is_never_listed_twice(shipped):
    cfg = _load(shipped, ["greenhouse", "ashby", "lever", "gem", "direct"])
    assert cfg.settings.enabled_platforms.count("gem") == 1


def test_gem_is_on_by_default_everywhere_it_is_declared():
    assert "gem" in config_mod.ScraperSettings().enabled_platforms
    shipped_yaml = (paths.SHIPPED_CONFIG / "scraper.yaml").read_text(encoding="utf-8")
    assert "    - gem\n" in shipped_yaml.replace("\r\n", "\n")
