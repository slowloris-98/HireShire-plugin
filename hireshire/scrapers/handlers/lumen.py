"""Lumen — careers.lumen.com, an Eightfold tenant. The walk is `eightfold.py`.

Lumen's long location form is the one to read: its `standardizedLocations`
turns "Remote, United States" into "Remote, OR, US", i.e. Oregon.
"""

from __future__ import annotations

from typing import Optional

from hireshire.models.job import Job
from hireshire.scrapers.handlers import eightfold

TENANT = eightfold.Tenant(
    token="lumen",
    label="Lumen",
    base="https://careers.lumen.com",
    domain="lumen.com",
)
TOKEN = TENANT.token
SCOPE_COLUMN = eightfold.SCOPE_COLUMN


def list_url(country: Optional[str], page: int) -> str:
    return eightfold.list_url(TENANT, country, page)


async def fetch_list(ctx, token: str) -> list[Job]:
    return await eightfold.fetch_list(TENANT, ctx)


async def fetch_detail(ctx, job: Job) -> Job:
    return await eightfold.fetch_detail(TENANT, ctx, job)
