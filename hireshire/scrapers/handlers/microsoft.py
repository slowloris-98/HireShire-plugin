"""Microsoft — apply.careers.microsoft.com, an Eightfold tenant.

Everything about the walk lives in `eightfold.py`, shared with Lumen and
Qualcomm; this module only names the tenant. `list_url`, `_countries` and
`_parse_job` keep their old one-tenant signatures for
`scripts/refresh_direct_locations.py` and the tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from hireshire.direct.scope import Scope
from hireshire.models.job import Job
from hireshire.scrapers.handlers import eightfold

TENANT = eightfold.Tenant(
    token="microsoft",
    label="Microsoft",
    base="https://apply.careers.microsoft.com",
    domain="microsoft.com",
)
TOKEN = TENANT.token
SCOPE_COLUMN = eightfold.SCOPE_COLUMN


def list_url(country: Optional[str], page: int) -> str:
    return eightfold.list_url(TENANT, country, page)


def _countries(scope: Optional[Scope]) -> list[Optional[str]]:
    return eightfold.countries(scope)


def _parse_job(entry: dict, scraped_at: datetime) -> Optional[Job]:
    return eightfold.parse_job(TENANT, entry, scraped_at)


async def fetch_list(ctx, token: str) -> list[Job]:
    return await eightfold.fetch_list(TENANT, ctx)


async def fetch_detail(ctx, job: Job) -> Job:
    return await eightfold.fetch_detail(TENANT, ctx, job)
