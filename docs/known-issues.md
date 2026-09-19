# Known issues — sweep of 2026-09-18

Source: `DATA/logs/orchestration.log` (sweeps of 2026-09-16 and 2026-09-18).

## Applier

| # | Issue | Reason | Status |
|---|-------|--------|--------|
| A1 | Resume upload refused; form not submitted (5 jobs) | Playwright MCP only reads files under the session cwd (`DATA/applied`); the resume lives in the workspace. `.mcp.json` runs `@playwright/mcp@latest`, so the restriction can arrive unannounced. | Fixed for sweeps in 0.5.0: the session runs in the workspace. `/hireshire:apply` still fails when launched outside it. |
| A2 | Same job applied to twice (roku, *Software Engineer, Embedded Agentic AI*) | The same title was tracked twice and queued twice; the second session only failed because of A1. Likely two job IDs for one requisition that were not clustered. | Open |
| A3 | Apply session failed to launch (rubrik, smartbear) | Same crash as S2. Recorded as deferred; retried next sweep. | Deferral was already correct. The log now names the code (`0xC0000142 STATUS_DLL_INIT_FAILED`). No retry, because a deferral is the right outcome for a heavyweight session. |
| A4 | Application stopped on a required question (c3iot, cloudflare, forthea) | The answer is not in the resume (graduation date, relocation, salary history). The job was recorded as `error`, then counted and listed as an application on the overview, so nobody noticed it. | Mitigated in 0.6.0: setup asks for authorization, sponsorship and relocation, and reads links off the resume. Essays, salary and start-date questions and tools the resume doesn't list are now answered. Anything still unsubmitted is listed under **Needs Attention** with a one-line reason and no longer counts as applied. |

## Scoring

| # | Issue | Reason | Status |
|---|-------|--------|--------|
| S1 | `claude CLI exited 1` — untrusted workspace (1 job, 2026-09-16) | The plugin shipped `.claude/settings.json`. | Fixed in `32df407` (0.5.0) |
| S2 | `claude CLI exited 3221225794: (no output)`; circuit breaker aborted scoring after 5 failures | `0xC0000142` (`STATUS_DLL_INIT_FAILED`): Windows could not start `claude.exe`. Not an API or auth error; no jobs were retired. **Not caused by concurrency:** the first failure (15:10:54, a 1-job batch) had no other call in flight, no apply session ran that cycle (all 10 pending jobs were at excluded companies), and the same monitor process had scored 31 jobs two hours earlier. Every call failed, and the log stops at 15:16:35 with no matcher summary. This points to the host: the sweep process lost the ability to start console children, for example because its hosting terminal or session ended, the machine was locked or asleep, or desktop heap ran out. | Mitigated in 0.5.1: launch failures are retried after 5/20/60 s before they count towards the breaker, and a breaker tripped by them blames the machine, not the backend. The underlying host condition is not fixed. |

## Proposed fixes

- **A1 (remaining):** make `/hireshire:apply` work when launched outside the workspace; pin the `@playwright/mcp` version.
- **A4 (done):** answer from setup's screening questions, then the resume, then the job description; list whatever is still blocked under Needs Attention. Still open: a job retired as `error` isn't retried after the user supplies the missing answer.
- **A2:** deduplicate the apply queue by requisition and check `applied` before each launch.
- **S2 (done):** retry `0xC0000142` with backoff. It still counts towards the breaker once retries are spent, because in the observed sweep every call failed, and not counting it would have burned the whole call budget ×4 against a host that could start nothing. Pausing scoring during apply or lowering `matcher.concurrency` is dropped: the log shows neither was involved.
- **Separate, still open:** the 12:49 failures (`exited 1`, zero-token envelope) are logged as the envelope's first 300 characters, which cuts off before its `result`/error text. Log `is_error` and `result` instead so the reason is visible.
