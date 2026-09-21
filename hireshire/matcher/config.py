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
    # Defaults match the default provider. `provider=None` resolves to claude_code,
    # so a model name from another vendor here made a fresh install fail on its first
    # scoring call for no reason the user could see.
    #
    # Sonnet rather than Haiku, deliberately, and not only for accuracy: `effort`
    # is unsupported on Haiku. The model-config docs list Fable, Opus 5/4.8/4.7,
    # Sonnet 5, Opus 4.6 and Sonnet 4.6, then say "models not listed here do not
    # support effort" — and it degrades silently, falling back to the highest
    # supported level at or below the one asked for. Shipping Haiku would mean
    # shipping an `effort` knob that does nothing. Haiku's 4,096-token minimum
    # cacheable prefix is the second reason: the rubric-plus-resume prefix built in
    # scorer.score would likely fall under it and silently never cache.
    #
    # The `codex` provider has no default here: setup pins one of the models
    # `codex debug models` lists, and CodexBackend refuses a Claude name outright.
    model: str = "sonnet"
    # Thinking level: low|medium|high|xhigh|max. Thinking tokens bill as output, and
    # once the resume prefix is cached they are the dominant cost of a sweep — which
    # matters because scoring draws on the same allowance as the user's own Claude
    # chat. Low is the default because the judge no longer reasons privately: the
    # evidence checklist and per-criterion rationales in ScoringSchema are that
    # reasoning, written out, bounded, and generated before each band. Measured on one
    # call at low: zero thinking tokens, 344 output. Medium is the fallback if verdicts
    # look shallow — measure a small run at each level before raising it.
    effort: str = "low"
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
    # Per-call bound for both CLI backends, claude_code and codex. The name predates
    # the second one and is kept because users' configs already carry it.
    claude_cli_timeout_s: float = 600.0
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
