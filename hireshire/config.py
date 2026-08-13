from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from hireshire import paths
from hireshire.rate_limit import RateLimiter


class CompanyConfig(BaseModel):
    name: str
    greenhouse_token: Optional[str] = None
    lever_token: Optional[str] = None
    ashby_token: Optional[str] = None
    bamboohr_token: Optional[str] = None
    workday_token: Optional[str] = None
    # Single-tenant career portals (Apple/Google/Intuit). The "token" is just the
    # company name — see hireshire/scrapers/direct.py.
    direct_token: Optional[str] = None
    tags: list[str] = []


class RateLimitConfig(BaseModel):
    concurrency: int = 10
    min_interval_s: float = 0.0  # min seconds between successive requests to this source


# Per-source defaults calibrated to each board's documented limits (see plan/Sources).
# Greenhouse Job Board API is cached/unlimited; Lever 10 rps steady; Ashby ~100/min;
# Workday/BambooHR throttle per-tenant so we also cap detail fan-out separately.
# These are FALLBACK defaults only — config/scraper.yaml's `settings.rate_limits` block
# overrides them (wholesale, no merge). Tune throttling there, not here.
_DEFAULT_RATE_LIMITS = {
    "greenhouse": RateLimitConfig(concurrency=20, min_interval_s=0.0),
    "lever": RateLimitConfig(concurrency=8, min_interval_s=0.0),
    "ashby": RateLimitConfig(concurrency=4, min_interval_s=0.5),
    "bamboohr": RateLimitConfig(concurrency=6, min_interval_s=0.0),
    "workday": RateLimitConfig(concurrency=8, min_interval_s=0.0),
    # Only 3 companies, all big-tech portals with no published limits — stay polite.
    "direct": RateLimitConfig(concurrency=3, min_interval_s=0.5),
}

# Per-board count of in-flight company workers. This is the company-level pool
# that drains each board's queue — decoupled from the per-call `rate_limits`
# semaphore so a company waiting its turn sits in an UNTIMED queue rather than
# burning its timeout budget. Fallback default only; override in scraper.yaml.
_DEFAULT_COMPANY_CONCURRENCY = {
    "greenhouse": 12,
    "lever": 8,
    "ashby": 4,
    "bamboohr": 5,
    "workday": 5,
    "direct": 3,
}


class ScraperSettings(BaseModel):
    concurrency: int = 10
    request_timeout_s: float = 30.0  # per-call (httpx) timeout; floored at 10s below
    retry_attempts: int = 3
    # Safety-net backstop per company. NOT the primary gate: the real per-call bound
    # is `request_timeout_s` (applied after the limiter is acquired, so it measures only
    # network time). This large cap only kills a genuinely wedged company and its clock
    # starts when a worker picks the company up — never during the untimed queue wait.
    company_timeout_s: float = 600.0
    max_age_hours: Optional[int] = None  # None = fetch all jobs regardless of age
    location_filter: list[str] = []      # empty = no filter; substring match against location + offices
    db_path: str = "hireshire.db"        # shared SQLite datastore; relative = under the plugin data dir

    # How often the orchestrator re-sweeps, in hours. Read by the monitor wrapper
    # (monitor commands cannot reference ${user_config.*} — Claude Code rejects
    # the monitor rather than substituting — so the value has to come from here).
    # Bounded below: a zero or negative interval turns the monitor into a continuous
    # sweep, which is how you get rate-limited off the boards the plugin depends on.
    poll_interval_hours: float = Field(4.0, gt=0)

    # The user's own job-search folder, captured ONCE by /hireshire:setup. Results
    # go to <workspace_dir>/hireshire_run_results/ and their resume is kept in
    # <workspace_dir>/resume/original/. It lives here for the same reason
    # poll_interval_hours does: the monitor needs it and cannot use ${user_config.*}.
    #
    # It is stored rather than derived from cwd because a plugin's cwd is whatever
    # project the user happens to be in — a session launched somewhere else must
    # still write to the folder they chose. Empty = fall back to DATA/results,
    # which is where installs that predate this setting keep writing.
    workspace_dir: str = ""

    # Which job boards to sweep. Workday (POST-based) and BambooHR (list->detail,
    # two requests per company) dominate run time — 14,798 more companies between
    # them — so they are opt-in. A disabled board's slug file is never even read.
    enabled_platforms: list[str] = Field(
        default_factory=lambda: ["greenhouse", "ashby", "lever", "direct"]
    )

    # Per-source throttling. Overridable via `settings.rate_limits` in config/scraper.yaml
    # (the YAML block replaces this dict wholesale). Sources absent from the map fall back
    # to a cap of `concurrency`.
    rate_limits: dict[str, RateLimitConfig] = Field(default_factory=lambda: dict(_DEFAULT_RATE_LIMITS))
    # Per-board count of in-flight company workers (the company-level pool that drains
    # each board's queue). Boards absent from the map fall back to `concurrency`.
    company_concurrency: dict[str, int] = Field(default_factory=lambda: dict(_DEFAULT_COMPANY_CONCURRENCY))
    # Per-tenant cap + jitter for list→detail boards (Workday, BambooHR) so one big
    # tenant can't flood its own host with hundreds of concurrent detail fetches.
    # Overridable via `settings.detail_concurrency` / `settings.detail_jitter_s` in scraper.yaml.
    detail_concurrency: int = 4
    detail_jitter_s: float = 0.3
    # List->detail boards (Workday, BambooHR) can defer the per-job detail fetch (the
    # description) to the matcher funnel, which only hydrates jobs that survive its
    # relevance gate. false = scrape list-only (content_text deferred); requires the
    # matcher funnel to be enabled or those jobs reach the scorer with no content.
    scrape_details: bool = True
    # Greenhouse's list API already returns job content, so the per-job detail
    # fetch only adds application `questions` (used by Phase 4). Off by default to
    # skip one HTTP call per job; enable when the applier needs question metadata.
    greenhouse_fetch_questions: bool = False
    # Pages to walk per direct career portal (Apple/Google/Intuit). They are all
    # sorted newest-first, so a small cap plus the age cutoff keeps runs cheap.
    direct_max_pages: int = 5

    @field_validator("request_timeout_s")
    @classmethod
    def _floor_request_timeout(cls, v: float) -> float:
        # Guarantee every API call gets at least a 10s window (user requirement).
        return max(10.0, v)

    @field_validator("workspace_dir")
    @classmethod
    def _workspace_must_be_absolute(cls, v: str) -> str:
        # Windows' "Copy as path" wraps the path in quotes, and users paste it whole.
        v = v.strip().strip('"').strip("'")
        if not v:
            return ""
        p = Path(v).expanduser()
        if not p.is_absolute():
            raise ValueError(
                f"workspace_dir must be an absolute path, got {v!r}. A relative "
                "value would resolve against the working directory, which is "
                "exactly what this plugin must never do — see hireshire/paths.py."
            )
        return str(p)

    def make_limiter(self, source: str) -> RateLimiter:
        cfg = self.rate_limits.get(source) or RateLimitConfig(concurrency=self.concurrency)
        return RateLimiter(cfg.concurrency, cfg.min_interval_s)

    def company_workers(self, source: str) -> int:
        return self.company_concurrency.get(source, self.concurrency)


class AppConfig(BaseModel):
    settings: ScraperSettings
    companies: list[CompanyConfig]

    @property
    def greenhouse_companies(self) -> list[CompanyConfig]:
        return [c for c in self.companies if c.greenhouse_token]

    @property
    def lever_companies(self) -> list[CompanyConfig]:
        return [c for c in self.companies if c.lever_token]

    @property
    def ashby_companies(self) -> list[CompanyConfig]:
        return [c for c in self.companies if c.ashby_token]

    @property
    def bamboohr_companies(self) -> list[CompanyConfig]:
        return [c for c in self.companies if c.bamboohr_token]

    @property
    def workday_companies(self) -> list[CompanyConfig]:
        return [c for c in self.companies if c.workday_token]

    @property
    def direct_companies(self) -> list[CompanyConfig]:
        return [c for c in self.companies if c.direct_token]


def _read_slugs(path: Path | None) -> list[str]:
    """Read a flat JSON array of slugs. `None` means the board is disabled and
    the file is never opened; a missing file is tolerated the same way."""
    if path is None or not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _load_companies_from_jsons(
    ashby_path: Path | None,
    greenhouse_path: Path | None,
    lever_path: Path | None,
    bamboohr_path: Path | None,
    workday_path: Path | None,
    direct_path: Path | None,
) -> list[CompanyConfig]:
    companies: list[CompanyConfig] = []
    for slug in _read_slugs(ashby_path):
        companies.append(CompanyConfig(name=slug, ashby_token=slug))
    for slug in _read_slugs(greenhouse_path):
        companies.append(CompanyConfig(name=slug, greenhouse_token=slug))
    for slug in _read_slugs(lever_path):
        companies.append(CompanyConfig(name=slug, lever_token=slug))
    for slug in _read_slugs(bamboohr_path):
        companies.append(CompanyConfig(name=slug, bamboohr_token=slug))
    for slug in _read_slugs(workday_path):
        # Workday slug is a compound 'company|wd#|site_id'; display the company part.
        companies.append(CompanyConfig(name=slug.split("|")[0], workday_token=slug))
    for slug in _read_slugs(direct_path):
        # Not a slug at all — the company name keys a handler module.
        companies.append(CompanyConfig(name=slug, direct_token=slug))
    return companies


def load_config(path: str | Path | None = None) -> AppConfig:
    path = Path(path) if path is not None else paths.config_file("scraper.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    settings = ScraperSettings(**raw.get("settings", {}))
    # Company lists are shipped, read-only assets and always live in ROOT — they do
    # NOT sit beside the YAML, which may be the user's copy in DATA.
    base = paths.SHIPPED_CONFIG
    enabled = set(settings.enabled_platforms)

    def _p(platform: str, filename: str) -> Path | None:
        # A disabled board's slug file is never read at all: skipping the five
        # big ones saves parsing ~600 KB of JSON per run.
        return (base / filename) if platform in enabled else None

    companies = _load_companies_from_jsons(
        ashby_path=_p("ashby", "ashby_companies.json"),
        greenhouse_path=_p("greenhouse", "greenhouse_companies.json"),
        lever_path=_p("lever", "lever_companies.json"),
        bamboohr_path=_p("bamboohr", "bamboohr_companies.json"),
        workday_path=_p("workday", "workday_companies.json"),
        direct_path=_p("direct", "direct_companies.json"),
    )
    return AppConfig(settings=settings, companies=companies)
