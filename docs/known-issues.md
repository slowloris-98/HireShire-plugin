# Known issues

Last verified against the code on 2026-09-22.
Source: `DATA/logs/orchestration.log` (sweeps of 2026-09-16 through 2026-09-22).

Only *outstanding* issues are listed. A fully resolved one is deleted rather than
marked fixed — the fix is in `CHANGELOG.md` and the git history, and a table of
things that are no longer true is a table nobody rereads. **IDs are stable and are
never reused**, so gaps are expected (A2, A3 and S1 were resolved and removed on
2026-09-22, A5 and R1 on 2026-09-22) and `CLAUDE.md`'s references to an issue by number
stay valid.

## Applier

| # | Issue | Reason | Status |
|---|-------|--------|--------|
| A1 | `@playwright/mcp` is unpinned | The original failure — resume upload refused because Playwright MCP only reads files under the session cwd — was fixed in 0.5.0 by running the session in the workspace, and the manual `/hireshire:apply` that still failed outside it was removed in 0.11.0. What remains is that `.mcp.json` runs `@playwright/mcp@latest`, so a future change to the file-access rules, the tool names or the CLI flags arrives unannounced, mid-sweep, on a user's machine. | Open. Symptom fixed, exposure not. |
| A4 | Application stopped on a required question (c3iot, cloudflare, forthea) | The answer is not in the resume (graduation date, relocation, salary history). | Mitigated in 0.6.0: setup asks for authorization, sponsorship and relocation and reads links off the resume; essays, salary and start-date questions and tools the resume doesn't list are now answered; anything still unsubmitted is listed under **Needs Attention** with a one-line reason and no longer counts as applied. **Still open:** a job retired as `error` is never retried once the user supplies the missing answer, so the row is permanent. |

## Scoring

| # | Issue | Reason | Status |
|---|-------|--------|--------|
| S2 | `claude CLI exited 3221225794: (no output)`; circuit breaker aborted scoring after 5 failures | `0xC0000142` (`STATUS_DLL_INIT_FAILED`): Windows could not start `claude.exe`. Not an API or auth error; no jobs were retired. **Not caused by concurrency:** the first failure (15:10:54, a 1-job batch) had no other call in flight, no apply session ran that cycle (all 10 pending jobs were at excluded companies), and the same monitor process had scored 31 jobs two hours earlier. Every call failed, and the log stops at 15:16:35 with no matcher summary. This points to the host: the sweep process lost the ability to start console children. | Mitigated in 0.5.1: launch failures are retried after 5/20/60 s (`scorer._LAUNCH_RETRY_DELAYS_S`, on both the Claude and Codex paths) before they count towards the breaker, and a breaker tripped by them blames the machine, not the backend. They **still count** towards the breaker once the retries are spent: in the observed sweep every call failed, and not counting them would have burned the whole call budget ×4 against a host that could start nothing. **The underlying host condition is not fixed.** 2026-09-21 (codex judge): it recurred at 20:29 and 21:35, with scoring and applying failing at the same moment while scraping carried on. **A locked screen is ruled out** — the 21:05 sweep ran almost entirely locked and launched ~30 apply sessions and 166 codex calls before failing. Sleep is ruled out too (no power events), and no leftover processes were found afterwards. Leading suspect: desktop heap exhaustion from everything running at once (VS Code Claude sessions, each with its own Playwright browser, plus the sweep's codex calls and apply sessions). Unproven: nothing records what else was running at the time. |
| S3 | Results CSV shows `llm_score` 0 for jobs never scored (10 rows, sweep `2026-09-21_202917`) | These are duplicates of a job whose scoring call failed (S2), so they copied its placeholder 0. `results_export._never_scored` returns `False` for **any** row carrying a `cluster_representative`, without asking whether that representative actually returned a verdict. The overview page is right for a different reason: `overview._job_entry` reads `skip_reason` directly rather than calling `_never_scored`. | Open. Note `reporting.data._never_scored` carries the identical bug — it is simply not on the path that renders the score — and `tests/test_reporting.py` pins the two copies together, so a fix has to change both. |

## Proposed fixes

- **A1:** pin the `@playwright/mcp` version, and bump it deliberately.
- **A4:** let the user clear a `Needs Attention` row so the job re-enters the apply
  queue once they have supplied the answer the session could not.
- **S2:** on a `0xC0000142` launch failure, log a count of running processes, so the
  next occurrence can confirm or rule out desktop-heap exhaustion. Pausing scoring
  during apply, and lowering `matcher.concurrency`, are both dropped: the log shows
  neither was involved.
- **S3:** in `_never_scored`, a duplicate should count as scored only if its
  representative actually got a verdict — the same check `overview._job_entry` makes.
  Both copies of the rule change together.
