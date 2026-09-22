# Known issues — sweep of 2026-09-18

Source: `DATA/logs/orchestration.log` (sweeps of 2026-09-16 and 2026-09-18).

## Applier

| # | Issue | Reason | Status |
|---|-------|--------|--------|
| A1 | Resume upload refused; form not submitted (5 jobs) | Playwright MCP only reads files under the session cwd (`DATA/applied`); the resume lives in the workspace. `.mcp.json` runs `@playwright/mcp@latest`, so the restriction can arrive unannounced. | Fixed in 0.5.0: the session runs in the workspace. The manual `/hireshire:apply`, which still failed outside it, was removed in 0.11.0. |
| A2 | Same job applied to twice (roku, *Software Engineer, Embedded Agentic AI*) | The same title was tracked twice and queued twice; the second session only failed because of A1. Likely two job IDs for one requisition that were not clustered. | Open |
| A3 | Apply session failed to launch (rubrik, smartbear) | Same crash as S2. Recorded as deferred; retried next sweep. | Deferral was already correct. The log now names the code (`0xC0000142 STATUS_DLL_INIT_FAILED`). No retry, because a deferral is the right outcome for a heavyweight session. |
| A4 | Application stopped on a required question (c3iot, cloudflare, forthea) | The answer is not in the resume (graduation date, relocation, salary history). The job was recorded as `error`, then counted and listed as an application on the overview, so nobody noticed it. | Mitigated in 0.6.0: setup asks for authorization, sponsorship and relocation, and reads links off the resume. Essays, salary and start-date questions and tools the resume doesn't list are now answered. Anything still unsubmitted is listed under **Needs Attention** with a one-line reason and no longer counts as applied. |

## Scoring

| # | Issue | Reason | Status |
|---|-------|--------|--------|
| S1 | `claude CLI exited 1` — untrusted workspace (1 job, 2026-09-16) | The plugin shipped `.claude/settings.json`. | Fixed in `32df407` (0.5.0) |
| S2 | `claude CLI exited 3221225794: (no output)`; circuit breaker aborted scoring after 5 failures | `0xC0000142` (`STATUS_DLL_INIT_FAILED`): Windows could not start `claude.exe`. Not an API or auth error; no jobs were retired. **Not caused by concurrency:** the first failure (15:10:54, a 1-job batch) had no other call in flight, no apply session ran that cycle (all 10 pending jobs were at excluded companies), and the same monitor process had scored 31 jobs two hours earlier. Every call failed, and the log stops at 15:16:35 with no matcher summary. This points to the host: the sweep process lost the ability to start console children, for example because its hosting terminal or session ended, the machine was locked or asleep, or desktop heap ran out. | Mitigated in 0.5.1: launch failures are retried after 5/20/60 s before they count towards the breaker, and a breaker tripped by them blames the machine, not the backend. The underlying host condition is not fixed. **2026-09-21 (codex judge):** it recurred at 20:29 and 21:35, with scoring and applying failing at the same moment, while scraping carried on. **A locked screen is ruled out:** the 21:05 sweep ran almost entirely locked and launched ~30 apply sessions and 166 codex calls before failing. Sleep is ruled out too (no power events), and no leftover processes from the sweep were found afterwards. Leading suspect: desktop heap exhaustion from everything running at once (VS Code Claude sessions, each with its own Playwright browser, plus the sweep's codex calls and apply sessions). Unproven: nothing records what else was running at the time. |
| S3 | Results CSV shows `llm_score` 0 for jobs never scored (10 rows, sweep `2026-09-21_202917`) | These are duplicates of a job whose scoring call failed (S2), so they copied its placeholder 0. `results_export._never_scored` counts any duplicate as scored. The overview page checks `skip_reason` and shows a dash correctly. | Open |

## Proposed fixes

- **A1 (remaining):** pin the `@playwright/mcp` version.
- **A4 (done):** answer from setup's screening questions, then the resume, then the job description; list whatever is still blocked under Needs Attention. Still open: a job retired as `error` isn't retried after the user supplies the missing answer.
- **A2:** deduplicate the apply queue by requisition and check `applied` before each launch.
- **S2 (done):** retry `0xC0000142` with backoff. It still counts towards the breaker once retries are spent, because in the observed sweep every call failed, and not counting it would have burned the whole call budget ×4 against a host that could start nothing. Pausing scoring during apply or lowering `matcher.concurrency` is dropped: the log shows neither was involved.
- **S2 (next):** on a `0xC0000142` launch failure, log a count of running processes, so the next occurrence can confirm or rule out desktop-heap exhaustion.
- **S3:** in `results_export._never_scored`, a duplicate should count as scored only if its representative actually got a verdict (same check the overview page uses).
- **Separate (done):** the `exited 1`, zero-token-envelope failures were logged as the envelope's raw first characters, which are `usage` and session bookkeeping and cut off before its `result`/error text. It cost a second night: nine apply sessions failed between 2026-09-21 23:38 and 2026-09-22 08:33 and every one logged its token counts and no reason. `claude_cli.envelope_failure` now reads `is_error`, `subtype`, `result` and — only when it is zero, the tell that the call never reached the model — `duration_api_ms`, and both claude paths report that in place of the raw stream, as the codex path already did. `claude_cli.exit_detail` (moved out of `scorer.py`) now covers the apply worker too, which had the `stderr or stdout` fallback that docstring exists to warn about.
