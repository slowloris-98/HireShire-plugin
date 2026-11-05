"""Qualcomm — careers.qualcomm.com, an Eightfold tenant. The walk is `eightfold.py`."""

from __future__ import annotations

from typing import Optional

from hireshire.models.job import Job
from hireshire.scrapers.handlers import eightfold

TENANT = eightfold.Tenant(
    token="qualcomm",
    label="Qualcomm",
    base="https://careers.qualcomm.com",
    domain="qualcomm.com",
)
TOKEN = TENANT.token
SCOPE_COLUMN = eightfold.SCOPE_COLUMN


def list_url(country: Optional[str], page: int) -> str:
    return eightfold.list_url(TENANT, country, page)


async def fetch_list(ctx, token: str) -> list[Job]:
    return await eightfold.fetch_list(TENANT, ctx)


async def fetch_detail(ctx, job: Job) -> Job:
    return await eightfold.fetch_detail(TENANT, ctx, job)
