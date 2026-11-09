"""Applied Materials — careers.appliedmaterials.com, an Eightfold tenant. The walk is
`eightfold.py`.

Its own search page scopes by `filter_country=United States of America,India`; the API's
`location=` takes one plain country name like every other tenant, so the user's scope is
walked one country at a time. Its locations are terse (`Santa Clara,CA`,
`Bangalore,IND`) and `normalize_location` reads them as they are.
"""

from __future__ import annotations

from typing import Optional

from hireshire.models.job import Job
from hireshire.scrapers.handlers import eightfold

TENANT = eightfold.Tenant(
    token="appliedmaterials",
    label="Applied Materials",
    base="https://careers.appliedmaterials.com",
    domain="appliedmaterials.com",
)
TOKEN = TENANT.token
SCOPE_COLUMN = eightfold.SCOPE_COLUMN


def list_url(country: Optional[str], page: int) -> str:
    return eightfold.list_url(TENANT, country, page)


async def fetch_list(ctx, token: str) -> list[Job]:
    return await eightfold.fetch_list(TENANT, ctx)


async def fetch_detail(ctx, job: Job) -> Job:
    return await eightfold.fetch_detail(TENANT, ctx, job)
