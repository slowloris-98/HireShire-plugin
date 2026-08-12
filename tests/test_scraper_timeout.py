"""Regression test for the scraper's timeout sequencing (worker-pool refactor).

The bug this guards against: the per-company timeout used to start the instant
`asyncio.gather` scheduled every company at once, so a company waiting behind the
per-board concurrency gate burned its whole timeout budget while still queued and
was killed before making a single API call.

The fix drains each board through a fixed pool of `company_concurrency` workers; a
company waiting its turn sits in an UNTIMED queue and the timeout clock only starts
when a worker picks it up. This test asserts that property end-to-end through the
real `scraper.main()`: with 2 workers, a tight per-company backstop, and 10 companies
whose combined work far exceeds that backstop, NOTHING times out and each company's
recorded fetch time reflects only its own work — not the time it spent queued.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import scraper
from hireshire.config import AppConfig, CompanyConfig, ScraperSettings

WORK_S = 0.2          # simulated per-company fetch time
BACKSTOP_S = 1.0      # per-company safety-net timeout (< total serial work of 10 companies)
WORKERS = 2
N_COMPANIES = 10


class _FakeScraper:
    """Stand-in scraper: every fetch_all takes WORK_S, tracking peak concurrency."""

    concurrent = 0
    peak = 0

    def __init__(self, *args, **kwargs):
        pass

    async def fetch_all(self, token: str):
        _FakeScraper.concurrent += 1
        _FakeScraper.peak = max(_FakeScraper.peak, _FakeScraper.concurrent)
        try:
            await asyncio.sleep(WORK_S)
            return []
        finally:
            _FakeScraper.concurrent -= 1


class _FakeStore:
    """In-memory RunStore: captures status + fetch_time_s per company."""

    def __init__(self, *args, **kwargs):
        self.ok: dict[str, float] = {}
        self.errors: dict[str, tuple[str, float]] = {}

    async def save_company(self, token, jobs, platform=None, fetch_time_s=None):
        self.ok[token] = fetch_time_s

    async def record_error(self, token, status, msg, platform=None, fetch_time_s=None):
        self.errors[token] = (status, fetch_time_s)

    async def finalise_run(self, started_at, stats=None):
        pass


def _make_config() -> AppConfig:
    settings = ScraperSettings(
        company_timeout_s=BACKSTOP_S,
        company_concurrency={"ashby": WORKERS},
        max_age_hours=None,
        location_filter=[],
    )
    companies = [CompanyConfig(name=f"co{i}", ashby_token=f"co{i}") for i in range(N_COMPANIES)]
    return AppConfig(settings=settings, companies=companies)


def test_queue_wait_does_not_count_against_timeout(monkeypatch, tmp_path):
    _FakeScraper.concurrent = 0
    _FakeScraper.peak = 0
    store = _FakeStore()

    monkeypatch.setattr(scraper, "load_config", lambda *_a, **_k: _make_config())
    monkeypatch.setattr(scraper, "AshbyScraper", _FakeScraper)
    monkeypatch.setattr(scraper, "RunStore", lambda *a, **k: store)
    monkeypatch.setattr(scraper, "get_db", lambda *a, **k: None)
    # Redirect all three slug files into tmp so the test neither reads the shipped
    # seed nor writes a delta into the real data dir.
    monkeypatch.setattr(scraper, "SEED_BAD_SLUGS_PATH", tmp_path / "seed_bad_slugs.json")
    monkeypatch.setattr(scraper, "USER_BAD_SLUGS_PATH", tmp_path / "user_bad_slugs.json")
    monkeypatch.setattr(scraper, "USER_RECOVERED_PATH", tmp_path / "user_recovered.json")

    start = time.monotonic()
    asyncio.run(scraper.main(quiet=True))
    wall = time.monotonic() - start

    # All companies succeeded — none starved out by the backstop.
    assert len(store.ok) == N_COMPANIES, f"unexpected errors: {store.errors}"
    assert not store.errors

    # The 10 companies ran 2-at-a-time, so total wall time exceeds the per-company
    # backstop — proving the backstop is NOT being consumed by queue-wait.
    assert wall > BACKSTOP_S

    # Each company's recorded fetch time reflects only its own work (~WORK_S),
    # never the time it spent waiting for a worker.
    assert all(t < BACKSTOP_S for t in store.ok.values())
    assert max(store.ok.values()) < WORK_S + 0.15

    # Worker pool actually capped in-flight companies at WORKERS.
    assert _FakeScraper.peak <= WORKERS


def test_request_timeout_floored_at_ten_seconds():
    # Per-call floor: config can never drop an API call below a 10s window.
    assert ScraperSettings(request_timeout_s=3).request_timeout_s == 10.0
    assert ScraperSettings(request_timeout_s=30).request_timeout_s == 30.0
