# Known issues

Last verified against the code on 2026-10-02.
Source: `DATA/logs/orchestration.log` (sweeps of 2026-09-16 through 2026-10-02).

Only *outstanding* issues are listed. A fully resolved one is deleted rather than
marked fixed — the fix is in `CHANGELOG.md` and the git history, and a table of
things that are no longer true is a table nobody rereads. **IDs are stable and are
never reused**, so gaps are expected (A2, A3 and S1 were resolved and removed on
2026-09-22, A5 and R1 on 2026-09-22, S4 on 2026-10-02) and `CLAUDE.md`'s references to an issue by number
stay valid.

## Applier

| # | Issue | Reason | Status |
|---|-------|--------|--------|
| A1 | `@playwright/mcp` is unpinned | The original failure — resume upload refused because Playwright MCP only reads files under the session cwd — was fixed in 0.5.0 by running the session in the workspace, and the manual `/hireshire:apply` that still failed outside it was removed in 0.11.0. What remains is that `.mcp.json` runs `@playwright/mcp@latest`, so a future change to the file-access rules, the tool names or the CLI flags arrives unannounced, mid-sweep, on a user's machine. | Open. Symptom fixed, exposure not. |
| A4 | Application stopped on a required question (c3iot, cloudflare, forthea) | The answer is not in the resume (graduation date, relocation, salary history). | Mitigated in 0.6.0: setup asks for authorization, sponsorship and relocation and reads links off the resume; essays, salary and start-date questions and tools the resume doesn't list are now answered; anything still unsubmitted is listed under **Needs Attention** with a one-line reason and no longer counts as applied. **Still open:** a job retired as `error` is never retried once the user supplies the missing answer, so the row is permanent. |
| A6 | Three stale backlog jobs can veto every fresh application in a sweep | `run_apply_worker` drains the backlog before the live queue (`worker.py:644-660`) and both share one breaker counter (`state["consecutive"]`/`state["tripped"]`, `worker.py:474`). The backlog returns `ORDER BY relevance_score DESC`, so the user's best stale jobs are tried first and three launch failures there trip the breaker before any freshly matched job is attempted. Observed on sweep `2026-10-02T03-41-54Z`: backlog failures at 20:41:56 / 20:42:07 / 20:42:17 (`0xC0000142`, i.e. S2), breaker tripped 21 s in, the scraper still short of 25% at 20:43:28, and `Applier done: 0 submitted, …, 5 deferred` at 20:49:38. A tripped breaker also suppresses the expiry pass (`worker.py:676`), so the same stale set leads the queue again next sweep. | Open. The breaker's question is "can this host launch sessions at all", and three stale jobs are a biased sample for it. |
| A7 | `Applier done: 0 submitted, 0 error, 0 deferred` does not distinguish starvation from failure | The applier is a `q4` consumer downstream of the matcher, and `load_pending_applications` returns only `shortlisted = 1` rows — a job the matcher never scored was never shortlisted, so it is invisible to the backlog as well. When the scoring breaker trips, the applier correctly has nothing to do, and its all-zero summary line reads as an applier failure. Five consecutive sweeps on 2026-10-02 (05:00, 06:07, 07:13, 08:19, 09:26) show the pair: matcher aborted scoring, applier reported all zeros. | Open. Diagnostic only — no work is lost, since the unscored jobs are rescored next sweep — but it is what made S2's host condition look like an applier bug. With S2 fixed in 0.23.0 the next all-zero line is *more* likely to be genuine starvation than a host fault, so the undistinguished summary gets marginally more misleading, not less. |
| A8 | An apply session's browser can outlive a clean exit | `terminate_apply_subprocess` is called only on timeout (`worker.py:411`) and cancellation (`:420`), never on a clean exit — and it returns early at `:156` once `returncode is not None`, so adding a post-exit call would be a **no-op**: by then `claude -p` has gone and its descendants are reparented, unreachable by that pid. Upstream microsoft/playwright-mcp#1568 (ending the client kills the MCP server and leaves Chrome running, because Chrome ignores SIGINT and SIGTERM) and anthropics/claude-code#67163 (the Windows variant, 1,220 orphaned node workers). Our `.mcp.json` is headed persistent-profile with no `--isolated`, i.e. the configuration #1568 describes. | **Open, and unmeasured on this install.** A process census on 2026-10-02 found 344 processes and **no** orphaned browser or node children on a machine that had been sweeping for days with auto-apply submitting, so this may not occur here at all. 0.23.0 adds the warning that will say: `run_apply_worker` already detected a live browser — `shutil.rmtree(..., ignore_errors=True)` leaving the scratch dir behind — and threw the signal away. |

## Scoring

| # | Issue | Reason | Status |
|---|-------|--------|--------|
| S2 | Every scoring call and apply session fails at once with `0xC0000142` while the sweep carries on scraping | **A stale inherited console.** A Windows child attaches to its parent's console by default, and `USER32.dll`'s initialisation attaches the process to that console's window station and desktop *before any user code runs*. When the console the sweeper inherited goes stale — the Claude Code session that started the background shell task ended or restarted, or its terminal was closed — that attach fails and **every** subsequent spawn dies in ~20 ms with no stdout and no stderr, while the parent is unaffected because it only does HTTP. (career-ops-hq/career-ops#3809 for the identical signature in a long-lived parent; openai/codex#46412 for the window-station mechanism.) **The evidence is per sweeper process, and an event rather than an accumulation**, which is what forces this reading: one process scored cleanly at 23:25, 00:33 and 01:39 on 2026-10-01/02, then failed every cycle from 02:46 through 09:26 and never recovered; the 10:10 restart cured it instantly, after which scoring failed with a real `out of credits` error and the applier submitted two applications. The 21:21 restart on 10-01 judged 43 jobs and submitted 1, and the 22:35 cycle in that same process failed. Onset was 3h20m into one process and ~74 min into another. | **Fixed in 0.23.0 — verification pending.** `claude_cli.own_console_kwargs()` gives every fully-piped child its own invisible console, so the parent's console lifetime stops mattering; `tests/test_child_console.py` pins that over the shipped spawn sites, and pins the two re-exec launchers that must keep inheriting stdio. **The old suspects are retired, not merely unproven:** desktop-heap exhaustion from concurrent load is ruled out alongside leaked processes, a locked screen and sleep — a census at 12:49 on 10-02 found 344 processes, zero orphaned CLI or browser children, and both `codex --version` and `claude --version` launching cleanly from a fresh console, and the onset times rule out any accumulation. The earlier note that a locked screen was already excluded (the 21:05 sweep ran almost entirely locked and made ~30 apply sessions and 166 codex calls before failing) now has a mechanism that explains it. Delete this row once a sweeper process has outlived the session that started it and gone on scoring cleanly. A separate lead from the same log, **not** this symptom: with `matcher.concurrency: 4`, concurrent `codex exec` processes race on one directory — `codex_skills_extension: failed to install system skills: Access is denied (os error 5)`. |
| S3 | Results CSV shows `llm_score` 0 for jobs never scored (10 rows, sweep `2026-09-21_202917`) | These are duplicates of a job whose scoring call failed (S2), so they copied its placeholder 0. `results_export._never_scored` returns `False` for **any** row carrying a `cluster_representative`, without asking whether that representative actually returned a verdict. The overview page is right for a different reason: `overview._job_entry` reads `skip_reason` directly rather than calling `_never_scored`. | Open. Note `reporting.data._never_scored` carries the identical bug — it is simply not on the path that renders the score — and `tests/test_reporting.py` pins the two copies together, so a fix has to change both. |

## Proposed fixes

- **A1:** pin the `@playwright/mcp` version, and bump it deliberately.
- **A4:** let the user clear a `Needs Attention` row so the job re-enters the apply
  queue once they have supplied the answer the session could not.
- **S2:** the fix shipped; what is left is the verification. Start a sweep, let one
  cycle score, then close the terminal or end the Claude Code session that launched it
  while leaving the sweeper alive, and confirm the next cycle still scores. Logging a
  process count is dropped — the census already ruled desktop-heap exhaustion out.
- **A6:** give the backlog its own breaker counter, or drain the live queue first, so
  that a stale job's launch failure cannot retire a sweep's fresh applications.
- **A7:** have the applier's summary line name its input — "queue empty (scoring
  aborted)" is a different fact from "every session failed", and only the first is
  someone else's bug.
- **A8:** a per-session Windows Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`,
  created around the spawn and closed in `apply_one`'s `finally` — not deferred to the
  outcome write, since the outcome comes from stdout that `communicate` has already
  returned, and killing earlier means the scratch `rmtree` runs with the browser's file
  handles released. It must never raise, must degrade silently to today's behaviour if
  `AssignProcessToJobObject` fails, and must log that degrade **once per sweep, not once
  per job**. Two things to write down rather than rediscover: assignment can only happen
  *after* `create_subprocess_exec` returns, so a grandchild spawned in that millisecond
  window escapes the job (`CREATE_SUSPENDED` → assign → `ResumeThread` is the correct
  fix and asyncio cannot express it), and nested jobs are fine since Windows 8, so a
  parent already inside someone else's job assigns as a nested one. Probe the cheap
  candidate first: `{{TOOL_PREFIX}}browser_close` at the end of `apply_one.md`, measured
  with `tasklist /FI "IMAGENAME eq chrome.exe"` before and after one real session — in
  persistent-profile mode it may close the page without ending the process, and a model
  may not comply, so it does not go into the one shared prompt on a guess.
- **S3:** in `_never_scored`, a duplicate should count as scored only if its
  representative actually got a verdict — the same check `overview._job_entry` makes.
  Both copies of the rule change together.
