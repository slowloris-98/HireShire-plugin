"""The shipped Eightfold tenant list, `config/eightfold_tenants.json`.

Four Eightfold tenants have a module of their own (`handlers/microsoft.py`,
`lumen.py`, `qualcomm.py`, `appliedmaterials.py`), because each carries a fact
about that tenant worth writing down. Every other tenant is one row in this file,
written only by `scripts/discover_eightfold.py`, which admits a tenant when its
own `/api/pcsx/search` answers with postings and never removes one.

This module is the only reader, and it is stdlib-only, so the three places that
need the list cannot disagree about it: the company loader (`config.py`), the
direct scraper's handler table (`scrapers/direct.py`), and the applier's
exclusions (`applier/config.py`). Like `direct_companies.json` it lives in ROOT,
so a release that adds tenants reaches every install with no migration.

Never raises. A missing or unreadable file means no extra tenants, and a row
missing a field is skipped with a warning rather than taking the others down.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from hireshire import paths

logger = logging.getLogger(__name__)

FILENAME = "eightfold_tenants.json"
FIELDS = ("token", "label", "base", "domain")


def entries(path: Optional[Path] = None) -> list[dict[str, str]]:
    """Every well-formed row, in file order, with each field stripped and
    `token` lowercased. `path` defaults to the shipped file."""
    path = path if path is not None else paths.SHIPPED_CONFIG / FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("No Eightfold tenants read from %s: %s", path, exc)
        return []
    if not isinstance(raw, list):
        logger.warning("%s is not a JSON list; no Eightfold tenants loaded", path.name)
        return []

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in raw:
        if not isinstance(row, dict) or not all(str(row.get(f) or "").strip() for f in FIELDS):
            logger.warning("Skipping malformed Eightfold tenant row in %s: %r", path.name, row)
            continue
        entry = {f: str(row[f]).strip() for f in FIELDS}
        entry["token"] = entry["token"].lower()
        entry["base"] = entry["base"].rstrip("/")
        if entry["token"] in seen:
            continue
        seen.add(entry["token"])
        out.append(entry)
    return out


def tokens(path: Optional[Path] = None) -> list[str]:
    return [e["token"] for e in entries(path)]
