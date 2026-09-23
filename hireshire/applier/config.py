from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel

from hireshire import paths

logger = logging.getLogger(__name__)


class ApplierSettings(BaseModel):
    # The single gate. True means the applier fills and SUBMITS real applications
    # after each pipeline run, unattended. There is no rehearsal mode: `dry_run` was
    # removed because a permanent rehearsal is indistinguishable from a broken
    # applier, which is what it turned out to be in practice.
    enable_applier: bool = False
    matches_dir: str = "matches"
    runs_dir: str = "scraped"
    db_path: str = "hireshire.db"
    resume_path: str = ""  # the user's own resume PDF; set by /hireshire:setup
    inter_job_delay_s: float = 10.0
    # One application's browser session is abandoned after this long. Recorded as an
    # error rather than retried: by then the form may already have been submitted.
    apply_timeout_s: float = 900.0
    # Each sweep also applies to jobs shortlisted this recently that have no
    # `applied` row — how a session that failed to launch gets retried, since the
    # matcher never streams a judged job twice.
    backlog_hours: int = 72
    max_steps: int = 40

    # Companies whose application forms sit behind an account login, so the
    # applier cannot complete them. Matched case-insensitively against a job's
    # board_token. Their tuned resumes are still generated for manual use.
    exclude_companies: list[str] = []

    # DERIVED, never user-set: copied from `scraper.location_filter` by
    # `load_applier_config`, and `applier.yaml` must not carry it. The apply session
    # re-checks the location because the scraper matched board metadata
    # (`job.location.name` plus `offices`) while the posting page may render something
    # shorter — `Arlington, VA, United States` scraped, `Arlington, VA` on the page.
    # There is one list so the two gates cannot disagree about what the user accepts.
    # Empty means no check at all, the same as the scraper.
    location_filter: list[str] = []

    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    # Read off the resume at setup and confirmed by the user; empty when it has none.
    linkedin_url: str = ""
    portfolio_url: str = ""

    # Screening answers the resume cannot give, asked once at setup. None means the
    # user was never asked — an install predating these — and `apply_one.md` falls
    # back to its old defaults for authorization and sponsorship.
    work_authorized: Optional[bool] = None
    requires_sponsorship: Optional[bool] = None
    willing_to_relocate: Optional[bool] = None

    generate_cover_letter: bool = True
    model: str = "gpt-4o-mini"


class ApplierConfig(BaseModel):
    settings: ApplierSettings


def _scraper_location_filter() -> list[str]:
    """`scraper.location_filter`, read straight out of the user's YAML.

    Never raises: an applier that cannot find the list simply does not check the
    location, which is what an empty list means everywhere else. Same trade as
    `hireshire.reporting.data._matcher_settings`, and it reads the YAML directly
    rather than going through `config.load_config`, which would also parse the
    company slug lists the applier has no use for.

    A bare string becomes a one-item list and blanks are dropped, mirroring
    `config_writer._as_str_list` — setup writes this field through that path, but an
    install predating it may have a hand-edited value.
    """
    try:
        raw = yaml.safe_load(
            paths.config_file("scraper.yaml").read_text(encoding="utf-8")
        ) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.debug("Could not read scraper.yaml for the location filter: %s", exc)
        return []
    value: Any = (raw.get("settings") or {}).get("location_filter") or []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def load_applier_config(path: str | Path | None = None) -> ApplierConfig:
    path = Path(path) if path is not None else paths.config_file("applier.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    settings = ApplierSettings(**raw.get("settings", {}))
    # Overwritten, not defaulted: whatever `applier.yaml` happens to carry loses to
    # the live scraper setting, because there is only one list and the scraper owns it.
    settings.location_filter = _scraper_location_filter()
    return ApplierConfig(settings=settings)
