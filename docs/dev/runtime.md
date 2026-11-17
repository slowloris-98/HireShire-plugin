# Runtime: sweep lifetime, launcher, venv, Windows

> Developer notes, moved verbatim from the root `CLAUDE.md`. Not loaded every
> session: the root file points here, and the nested `CLAUDE.md` beside the code
> imports it when Claude works in that directory.

## The recurring sweep

- **The recurring sweep is NOT session-scoped, and nothing may make it so again.**
  `scripts/run_orchestration.py` is an ordinary sleep/sweep loop. `--monitor` runs it
  recurring, `--sweep` runs one cycle (`--once`) and is what the OS scheduler entry
  uses — one program, so a fix to either reaches both. `/hireshire:start-orchestration`
  is the only sweep command; the one-shot `/hireshire:find-jobs` and the manual
  `/hireshire:apply` were removed in 0.11.0, so do not bring either back. Three
  rules survive from the old design: it reads `poll_interval_hours` from the user's
  config itself (`orchestrate.py --interval` defaults to 4 and never looks); every
  stdout line reaches the agent, so it emits one summary line per cycle and logs the
  rest to a file — which is also why the re-exec hop must keep inheriting stdio; see the
  console bullet below; and nothing may detach the process.

  **Two attempts to tie it to a session both destroyed users' work, and the second is
  why the idea is abandoned rather than refined.** The first passed `$$`/`$PPID` from
  Git Bash to a watchdog — MSYS keeps its own pid namespace (`ps` reports 1684 for a
  shell Windows calls WINPID 14072) while `is_alive` asks Win32 `OpenProcess`, so two
  meaningless numbers read as dead and killed a healthy sweep 60 seconds in. The second
  read `CLAUDE_PID` instead, paired with a `SessionEnd` hook that stopped whatever the
  status file named.

  That hook fired for **every** Claude Code session ending anywhere on the machine —
  including the short-lived `claude -p` sessions the scorer spawns per scoring call. An
  ownership check was added and did not save it: on a fresh Windows install `CLAUDE_PID`
  was simply **absent**, so the sweeper recorded no owner, the check's null-owner
  fallback answered "stop it" for every ending session, and the sweep manufactured its
  own killers. Observed: `session_pid: null`, `heartbeat - started_at` of **60.34 s**
  (one beat, then nothing), exit code 1, no traceback, no `ERROR` line — because
  `taskkill /T /F` unwinds nothing. It hit `--once` too, so the one-shot and scheduled
  routes died the same way.

  The lesson is not "find a better session signal". It is that a sweep must not act on
  host-specific identity it cannot verify, because the absence of that identity is
  indistinguishable from a legitimate reap and the failure direction is destroying work.
  Both layers were first replaced by a 24-hour runtime bound (`_MAX_RUNTIME_S`). That
  bound is gone too, because it stopped sweeps users wanted running. A recurring sweep
  now runs until `--stop` or until its process is killed, so **unattended auto-apply has
  no time limit, by design**. Do not bring back a session tie to bound it, and
  `tests/test_sweep_lifetime.py` fails if the bound reappears. `--stop` is the only
  deliberate stop, and surviving a closed terminal *on purpose* is the OS scheduler
  entry `/hireshire:setup` offers.

  Consequences that should not be re-derived:

  - **There is no `--status`, and the skill must not invent one.** `hireshire/sweep_pid.py`
    records one integer; `hireshire/orchestration_status.py` — heartbeat, `STALE_AFTER_S`,
    the liveness veto, `describe()` — is gone with the teardown it served. The user
    watches a sweep through the overview page or their shell task.
  - **`/hireshire:start-orchestration` must not claim the sweep stops with the session.**
    It no longer does. It ends on `--stop` or on the shell task being killed, and it must
    not promise a time limit either. Saying otherwise is the same class of failure as announcing a sweep that was
    never running, and `tests/test_plugin_shell.py` pins the skill's wording.
  - **`process_liveness.is_alive` survives, for one caller only:** the guard that refuses
    to start a second writer onto the same SQLite database. It was never the bug — it
    answers correctly about a pid the plugin recorded **about itself**. Every failure
    here came from feeding it an identity that had been guessed at. A recorded pid that
    is definitively gone must not block a new sweep, which is why the guard probes rather
    than trusting the file.
  - **Killing the leaf is enough, because the chain unwinds itself.** Every parent in the
    re-exec chain is blocked in `subprocess.run`, so each exits as soon as its child does
    — verified against a live four-process orphan, where a `taskkill /T` on the recorded
    leaf cleared all four. `--stop` on POSIX must signal the recorded pid **itself**:
    `pkill -P` only ever reaches children, and gating the SIGTERM on pkill having *failed*
    meant the sweeper survived precisely when it had a child, which is during the apply
    phase.
  - `tests/test_sweep_lifetime.py` fails the build if `run_orchestration.py` so much as
    names `CLAUDE_PID` in code, and `tests/test_plugin_shell.py` fails it if a
    `SessionEnd` hook reappears.

## Launcher, PyTorch, child processes

- **Interpreter discovery lives in exactly one place: `scripts/hireshire.sh`.**
  Two traps make this worth centralising. macOS has no bare `python` — Apple
  removed `/usr/bin/python` in 12.3 and Homebrew installs `python3` only. And
  Windows ships a Microsoft Store App Execution Alias named `python3.exe` that
  *exists on PATH*, prints an ad and exits 49, so `command -v python3` selects the
  broken one while the real `python` sits beside it. The launcher therefore
  **runs** each candidate and keeps the first reporting Python ≥ 3.10. Hooks,
  monitors and both skills go through it; nothing else may name
  an interpreter. Windows needs Git Bash so `sh` exists.
- **PyTorch comes from `uv pip install --torch-backend auto`, never a hand-kept tag
  list.** Off macOS, `bootstrap._install_with_uv` pip-installs uv (unpinned, so it
  knows the current PyTorch index tags, which change every release) and installs the
  whole requirements file through it. sentence-transformers then picks cuda → mps →
  cpu on its own; there is no `device` setting. Four rules keep a GPU upgrade from
  costing a working venv:
  - **Only an architecture mismatch downgrades to CPU.** uv reads the driver version,
    not compute capability (astral-sh/uv#14742), so the smoke test runs a matmul on the
    device. `bad_kernels` reinstalls CPU and writes the `_ARCH_FALLBACK` marker to
    `DATA/torch_variant`, which pins `cpu` from then on. `no_gpu` (a CUDA build, CUDA
    unavailable) is **kept**: it runs on CPU anyway, and treating a transient fault as a
    verdict would pin the install to CPU for good. The marker is only cleared by hand
    or by `HIRESHIRE_TORCH`.
  - **Never swap torch under a live sweep.** Bootstrap runs *before*
    `run_orchestration`'s duplicate guard and before every `run_engine` call, and on
    Windows replacing torch's DLLs under a running process half-removes it. So
    `main()` checks `sweep_pid` + `is_alive` first and defers.
  - **A failed download removes nothing, and only-the-torch-line-changed soft-fails.**
    uv downloads before replacing. If the requirements are otherwise unchanged and
    torch still imports, `main()` returns 0 with the lock unwritten, so an offline
    machine keeps sweeping and retries next start.
  - **The lock records the backend *requested*, not what uv resolved**, so
    `is_current()` never probes a GPU on SessionStart. A CPU `torch` is named for
    `--reinstall-package` only when `nvidia-smi`/`rocm-smi` exists, or every GPU-less
    install would re-download it once.

  Precision stays fp32 on every device, and that is load-bearing: `min_score` is a raw
  logit, and half precision would shift it. Out of memory in `Reranker._score` halves
  the batch (and keeps it halved), then swaps to a separately cached CPU copy — never
  `model.to()` on the shared one.
- Downstream of the launcher, `scripts/run_engine.py` re-execs into the venv and
  addresses its interpreter by absolute path — hook exec form cannot spawn the
  `.cmd`/`.bat` shims Windows installs.
- **Every child the engine starts with fully piped stdio gets its own invisible console
  on Windows; the two re-exec launchers must never get one.**
  `claude_cli.own_console_kwargs()` is the single place that decides, and it returns
  `creationflags` and nothing else — the moment it carries `stdout`, `env` or `cwd` the
  exclusion below becomes unstateable. A Windows child inherits its parent's console by
  default, and `USER32.dll`'s init attaches the process to that console's window station
  and desktop *before any user code runs*, so when the console the sweeper inherited
  goes stale (its Claude Code session ended or restarted, its terminal was closed) every
  subsequent spawn dies with `0xC0000142` in ~20 ms with no stdout and no stderr — while
  the parent, which only does HTTP, carries on scraping.

  **It is per process and an event, not per sweep and not an accumulation**, and that is
  what retired the old diagnosis. Measured: one sweeper scored cleanly at 23:25, 00:33
  and 01:39, then failed every cycle from 02:46 to 09:26 and never recovered; a restart
  cured it instantly. Onset was 3h20m into that process and ~74 min into another, and a
  census found 344 processes with zero orphaned children — so desktop heap, leaked
  processes, a locked screen and sleep are all ruled out, and `docs/known-issues.md` S2's
  original suspects were wrong. `DETACHED_PROCESS` is the wrong flag: with no console at
  all, `npx` and `cmd` grandchildren may allocate **visible** ones on the user's desktop.

  **The exclusion is `run_orchestration._reexec_in_venv` and `run_engine.run`**, which
  pass no `stdout`/`stderr` and must keep inheriting: the child's one-line-per-cycle
  `print(..., flush=True)` is the only thing the background shell task surfaces to the
  agent, so a new console there would silently sever the sweep's output from the user —
  the same failure class as announcing a sweep that was never running. Getting it wrong
  on a piped site costs a dead child that logs an exit code; getting it wrong there costs
  silence, which is why the rule is "every piped site, mechanically" rather than "the
  ones we think are at risk" — whose console is stale is not knowable at the call site.
  `tests/test_child_console.py` pins both halves by AST over the shipped spawn sites, so
  a new spawn site cannot be added without answering the question, and it asserts the two
  exempt calls pipe *nothing*, so piping one of them fails the build rather than quietly
  reclassifying it.

  Note this is **spawn kwargs, not CLI flags.** `scorer.py` is right that `--safe-mode`
  and `--tools ""` must never be shared through `claude_cli` — the applier needs tools
  and an MCP server — and that rule is about argv, which decides what the model can do.
  This decides whether Windows will start the process at all. The judge and the applier
  want identical spawn kwargs and different argv, and the two policies do not touch;
  `tests/test_apply_worker.py` asserts both halves in one body so they read as a pair.
  For the same reason `scorer._reap` kills the **leaf** rather than the tree: a judge
  session has no children by construction, and a tree-wide kill on Windows *means
  spawning `taskkill`*, which is subject to the very failure this bullet is about.

