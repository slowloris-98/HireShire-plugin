"""A slug with no board must not break the sweep, and must not be skipped later.

There is deliberately no dead-slug skip list. The seed-plus-delta scheme that used
to be here (`config/bad_slugs.json` in ROOT, `user_bad_slugs.json` /
`user_recovered_slugs.json` in DATA) was read once before a sweep and never
re-checked during one, so it could only grow: a slug that 404'd through a transient
outage was skipped on every future sweep, and the only road back was a terminal
command. These tests pin both halves of the replacement — a 404 is contained to the
one company, and it is still tried on the next run.
"""
from __future__ import annotations

import asyncio

import scraper
from hireshire import paths
from hireshire.config import AppConfig, CompanyConfig, ScraperSettings
from hireshire.scrapers.exceptions import SlugNotFoundError

DEAD = "gone-co"
LIVE = ("live-a", "live-b", "live-c")


class _FakeScraper:
    """Raises SlugNotFoundError for DEAD, returns one job for everything else."""

    attempts: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def fetch_all(self, token: str):
        _FakeScraper.attempts.append(token)
        if token == DEAD:
            raise SlugNotFoundError("ashby", token)
        return [object()]  # the store is faked, so the job's shape is irrelevant


class _FakeStore:
    def __init__(self, *args, **kwargs):
        self.ok: dict[str, int] = {}
        self.errors: dict[str, tuple[str, str]] = {}
        self.done = 0
        self.total: int | None = None

    async def set_companies_total(self, total):
        self.total = total

    async def company_done(self):
        self.done += 1

    async def save_company(self, token, jobs, platform=None, fetch_time_s=None):
        self.ok[token] = len(jobs)

    async def record_error(self, token, status, msg, platform=None, fetch_time_s=None):
        self.errors[token] = (status, msg)

    async def finalise_run(self, started_at, stats=None):
        self.stats = dict(stats or {})


def _config() -> AppConfig:
    settings = ScraperSettings(
        company_concurrency={"ashby": 2},
        max_age_hours=None,
        location_filter=[],
    )
    companies = [
        CompanyConfig(name=t, ashby_token=t) for t in (LIVE[0], DEAD, LIVE[1], LIVE[2])
    ]
    return AppConfig(settings=settings, companies=companies)


def _run(monkeypatch) -> _FakeStore:
    store = _FakeStore()
    monkeypatch.setattr(scraper, "load_config", lambda *a, **k: _config())
    monkeypatch.setattr(scraper, "AshbyScraper", _FakeScraper)
    monkeypatch.setattr(scraper, "RunStore", lambda *a, **k: store)
    monkeypatch.setattr(scraper, "get_db", lambda *a, **k: None)
    asyncio.run(scraper.main(quiet=True))
    return store


def test_a_slug_with_no_board_does_not_break_the_sweep(monkeypatch):
    _FakeScraper.attempts = []
    store = _run(monkeypatch)

    # Every other company still fetched and saved.
    assert set(store.ok) == set(LIVE), f"live companies lost: {store.ok}"

    # The dead one is recorded under its own status, separable from a real failure
    # so nobody reads it as a board that errored.
    assert store.errors[DEAD][0] == "not_found"
    assert DEAD not in store.ok

    # The scraper bar still balances: every company counted done exactly once.
    assert store.total == len(LIVE) + 1
    assert store.done == len(LIVE) + 1

    # ...and it is counted apart from `errors`, which drives the console summary.
    assert store.stats["not_found"] == 1
    assert store.stats["errors"] == 0


def test_a_slug_with_no_board_is_tried_again_on_the_next_run(monkeypatch):
    """The whole point of removing the skip list: a 404 is a deferral, not a verdict."""
    _FakeScraper.attempts = []
    _run(monkeypatch)
    _run(monkeypatch)

    assert _FakeScraper.attempts.count(DEAD) == 2, (
        "a slug that 404'd was skipped on the second run — the skip list is back"
    )


def test_the_skip_list_has_not_come_back():
    """Guards the reversal itself: these names are how it was spelled before."""
    for gone in ("SEED_BAD_SLUGS_PATH", "USER_BAD_SLUGS_PATH", "USER_RECOVERED_PATH",
                 "_load_bad_slugs", "_save_bad_slugs"):
        assert not hasattr(scraper, gone), f"{gone} is back — see the note in scraper.py"

    assert not (paths.SHIPPED_CONFIG / "bad_slugs.json").exists()
