"""The discovery wrapper only forwards the shared flags and reports the worst exit code."""
from __future__ import annotations

import sys

from hireshire import paths

sys.path.insert(0, str(paths.ROOT / "scripts"))

import discover_all  # noqa: E402


def test_every_tool_runs_in_order_with_the_shared_flags(monkeypatch):
    calls = []
    for tool, code in zip(discover_all.TOOLS, (0, 1, 0)):
        monkeypatch.setattr(tool, "main", lambda argv, t=tool, c=code: calls.append((t, argv)) or c)

    status = discover_all.main(["--crawls", "1", "--cdx-attempts", "2", "--write"])

    forwarded = ["--crawls", "1", "--cdx-attempts", "2", "--write"]
    assert calls == [(t, forwarded) for t in discover_all.TOOLS]   # a failure stops nothing
    assert status == 1
