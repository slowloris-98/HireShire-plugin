"""The shipped Oracle Recruiting Cloud employer list, `config/oracle_companies.json`.

An Oracle Recruiting Cloud employer is a **pod** — its own Oracle HCM server,
`<pod>.fa.<region>.oraclecloud.com` — carrying one or more candidate sites
(`CX_1`, `BDOExperiencedCareers`, …). So a row is

    {"token": "bdo-usa", "label": "BDO USA", "host": "ebqb.fa.us2.oraclecloud.com",
     "sites": ["BDOExperiencedCareers", "CX_1"]}

and not a slug. `token` is a readable name because the dashboard and the results
CSV print `board_token` as the company. Rows are written only by
`scripts/discover_oracle.py`, which admits a pod when its own requisition API
answers with jobs and never removes one.

This module is the only reader, and stdlib-only, so the three places that need the
list agree about it: the company loader (`config.py`), the scraper
(`scrapers/oracle.py`) and the applier's exclusions (`applier/config.py`). The list
lives in ROOT, so a release that adds employers reaches every install.

Never raises. A missing or unreadable file means no Oracle employers; a malformed
row is skipped with a warning, and a repeated token keeps its first row.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from hireshire import paths

logger = logging.getLogger(__name__)

FILENAME = "oracle_companies.json"


def entries(path: Optional[Path] = None) -> list[dict]:
    """Every well-formed row, in file order: `token` lowercased, `host` lowercased,
    `sites` a non-empty list of non-blank strings."""
    path = path if path is not None else paths.SHIPPED_CONFIG / FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("No Oracle employers read from %s: %s", path, exc)
        return []
    if not isinstance(raw, list):
        logger.warning("%s is not a JSON list; no Oracle employers loaded", path.name)
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for row in raw:
        entry = _clean(row)
        if entry is None:
            logger.warning("Skipping malformed Oracle employer row in %s: %r", path.name, row)
            continue
        if entry["token"] in seen:
            continue
        seen.add(entry["token"])
        out.append(entry)
    return out


def _clean(row) -> Optional[dict]:
    if not isinstance(row, dict):
        return None
    token = str(row.get("token") or "").strip().lower()
    label = str(row.get("label") or "").strip()
    host = str(row.get("host") or "").strip().lower()
    sites = row.get("sites")
    if not (token and label and host) or not isinstance(sites, list):
        return None
    sites = [str(s).strip() for s in sites if str(s).strip()]
    if not sites:
        return None
    return {"token": token, "label": label, "host": host, "sites": sites}


def tokens(path: Optional[Path] = None) -> list[str]:
    return [e["token"] for e in entries(path)]


def by_token(path: Optional[Path] = None) -> dict[str, dict]:
    return {e["token"]: e for e in entries(path)}
