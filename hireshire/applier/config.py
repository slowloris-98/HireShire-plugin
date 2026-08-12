from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from hireshire import paths


class ApplierSettings(BaseModel):
    enable_applier: bool = False  # orchestrator default: run the applier after each pipeline run
    dry_run: bool = True
    matches_dir: str = "matches"
    applied_dir: str = "applied"
    runs_dir: str = "scraped"
    db_path: str = "hireshire.db"
    resume_path: str = ""  # the user's own resume PDF; set by /hireshire:setup
    headless: bool = True
    inter_job_delay_s: float = 10.0
    max_steps: int = 40

    # Companies whose application forms sit behind an account login, so the
    # applier cannot complete them. Matched case-insensitively against a job's
    # board_token. Their tuned resumes are still generated for manual use.
    exclude_companies: list[str] = []

    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""

    generate_cover_letter: bool = True
    model: str = "gpt-4o-mini"


class ApplierConfig(BaseModel):
    settings: ApplierSettings


def load_applier_config(path: str | Path | None = None) -> ApplierConfig:
    path = Path(path) if path is not None else paths.config_file("applier.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ApplierConfig(settings=ApplierSettings(**raw.get("settings", {})))
