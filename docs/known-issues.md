# Known issues — sweep of 2026-09-18

Source: `DATA/logs/orchestration.log` (sweeps of 2026-09-16 and 2026-09-18).

## Applier

| # | Issue | Reason | Status |
|---|-------|--------|--------|
| A1 | Resume upload refused; form not submitted (5 jobs) | Playwright MCP only reads files under the session cwd (`DATA/applied`); the resume lives in the workspace. `.mcp.json` runs `@playwright/mcp@latest`, so the restriction can arrive unannounced. | Fixed for sweeps in 0.5.0: the session runs in the workspace. `/hireshire:apply` still fails when launched outside it. |
| A2 | Same job applied to twice (roku, *Software Engineer, Embedded Agentic AI*) | The same title was tracked twice and queued twice; the second session only failed because of A1. Likely two job IDs for one requisition that were not clustered. | Open |
| A3 | Apply session failed to launch (rubrik, smartbear) | Same crash as S2. Recorded as deferred; retried next sweep. | Open (see S2) |
| A4 | Application stopped on a required question (c3iot, cloudflare, forthea) | The answer is not in the resume (graduation date, relocation, salary history). Intended behaviour. | Not a bug |

## Scoring

| # | Issue | Reason | Status |
|---|-------|--------|--------|
| S1 | `claude CLI exited 1` — untrusted workspace (1 job, 2026-09-16) | The plugin shipped `.claude/settings.json`. | Fixed in `32df407` (0.5.0) |
| S2 | `claude CLI exited 3221225794: (no output)`; circuit breaker aborted scoring after 5 failures | `0xC0000142` (`STATUS_DLL_INIT_FAILED`): Windows could not start `claude.exe`. Most likely resource exhaustion from concurrent processes: 4 parallel scorers plus an apply session (claude, node, Chromium). Not an API or auth error. No jobs were retired. | Open |

## Proposed fixes

- **A1 (remaining):** make `/hireshire:apply` work when launched outside the workspace; pin the `@playwright/mcp` version.
- **A2:** deduplicate the apply queue by requisition and check `applied` before each launch.
- **S2:** treat `0xC0000142` as transient (wait and retry; do not count it towards the circuit breaker); pause scoring while an apply session is running, or lower `matcher.concurrency` on Windows.
