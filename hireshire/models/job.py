from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup
from pydantic import BaseModel, HttpUrl, field_validator


class Location(BaseModel):
    name: str


class Department(BaseModel):
    id: int
    name: str
    parent_id: Optional[int] = None


class Office(BaseModel):
    id: int
    name: str
    location: Optional[str] = None


class ApplicationQuestion(BaseModel):
    label: str
    required: bool
    field_type: str
    values: list[str] = []


class Job(BaseModel):
    source: str
    board_token: str
    job_id: str
    internal_job_id: Optional[str] = None

    title: str
    location: Location
    departments: list[Department] = []
    offices: list[Office] = []
    absolute_url: HttpUrl
    updated_at: datetime
    requisition_id: Optional[str] = None

    content_text: Optional[str] = None
    # For list->detail boards scraped in list-only mode (content_text deferred to
    # the matcher funnel): the source-specific key needed to re-fetch the detail
    # page. Workday stores its `externalPath` here (job_id is not always the path);
    # BambooHR needs none (rebuilds from board_token + job_id).
    detail_path: Optional[str] = None

    questions: list[ApplicationQuestion] = []
    detail_fetch_failed: bool = False

    scraped_at: datetime

    @field_validator("content_text", mode="before")
    @classmethod
    def strip_html(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        return BeautifulSoup(v, "lxml").get_text(separator=" ", strip=True)

    @field_validator("updated_at", "scraped_at", mode="after")
    @classmethod
    def ensure_utc(cls, v: datetime) -> datetime:
        # Date-only sources (Workday startDate, BambooHR datePosted) parse as
        # naive; assume UTC so they stay comparable to the tz-aware age cutoff.
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v
