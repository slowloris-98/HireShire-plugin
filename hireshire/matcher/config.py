from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from hireshire import paths
from hireshire.funnel.config import FunnelConfig


class MatcherSettings(BaseModel):
    # Bounded so a value meant for the funnel's 0-1 cosine gate cannot land here, and
    # vice versa — the two settings live in the same file and read alike.
    threshold: int = Field(70, ge=0, le=100)  # min relevance_score to shortlist
    concurrency: int = 1
    provider: str | None = None  # None = fall back to LLM_PROVIDER env var
    model: str = "gemini-2.0-flash"
    effort: str = "medium"  # claude_code thinking level: low|medium|high|xhigh|max
    max_content_chars: int = 8000
    resume_path: str = "resume.pdf"
    projects_path: str = ""  # optional markdown file appended to candidate profile
    # Expanded "ideal candidate" profile generated at setup. Used ONLY as the
    # reranker query — never fed to the scorer, which must judge against the real
    # resume rather than transferable-skill framing. See funnel/rerank.py.
    search_profile_path: str = ""
    runs_dir: str = "scraped"
    matches_dir: str = "matches"
    db_path: str = "hireshire.db"
    request_interval_s: float = 13.0  # min seconds between requests; 13s = ~4.6 RPM (safe for 5 RPM free tier)
    claude_cli_timeout_s: float = 600.0  # per-call bound for the claude_code backend
    skip_llm: bool = False

    @field_validator("effort")
    @classmethod
    def _check_effort(cls, v: str) -> str:
        allowed = {"low", "medium", "high", "xhigh", "max"}
        if v not in allowed:
            raise ValueError(f"effort must be one of {sorted(allowed)}, got {v!r}")
        return v


class TitleFilterConfig(BaseModel):
    include_keywords: list[str] = []  # title must match at least one (if non-empty)
    exclude_keywords: list[str] = []  # title must match none


class MatcherConfig(BaseModel):
    settings: MatcherSettings
    title_filter: TitleFilterConfig = TitleFilterConfig()
    funnel: FunnelConfig = FunnelConfig()


def load_matcher_config(path: str | Path | None = None) -> MatcherConfig:
    path = Path(path) if path is not None else paths.config_file("matcher.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return MatcherConfig(
        settings=MatcherSettings(**raw.get("settings", {})),
        title_filter=TitleFilterConfig(**raw.get("title_filter", {})),
        funnel=FunnelConfig(**raw.get("funnel", {})),
    )
