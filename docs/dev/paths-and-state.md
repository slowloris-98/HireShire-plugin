# Paths and state: ROOT, DATA, WORKSPACE

> Developer notes, moved verbatim from the root `CLAUDE.md`. Not loaded every
> session: the root file points here, and the nested `CLAUDE.md` beside the code
> imports it when Claude works in that directory.

### The ROOT/DATA/WORKSPACE split governs where every file goes

**ROOT** is the install dir and is **replaced wholesale on every plugin update** —
shipped, read-only content only: engine code, default YAMLs, company slug lists.
**DATA** (`~/.claude/plugins/data/hireshire-hireshire/`)
**survives updates** — venv, SQLite DB, the user's config, generated profile, logs.

**Putting mutable state in ROOT loses it on the next update.** `hireshire/paths.py`
is the single place this is decided; nothing else may resolve a path against the
working directory, because a plugin's cwd is whatever project the user is in.

**One namer, one creator, for the day layout.** `paths.run_dir_for(stamp)` decides
where a run folder goes and creates nothing; `make_run_dir` creates what it names, and
`orchestrate.finalise_abandoned_runs` uses it to find a killed sweep's folder again. A
flat `<root>/<stamp>` that **already exists wins**, which is what keeps run folders made
before the day layout exactly where the user left them — there is no migration, nothing
moves inside the user's own workspace, and no `file://` bookmark breaks. A stamp is
second-resolution, so that probe can only ever match a genuinely older run. The day
comes from `run_ids.day_of(stamp)`, which **validates** rather than slicing: an
unparseable run id makes `finalise_abandoned_runs` fall back to `stamp = run_id`, and
`""` is the signal to use the flat layout rather than build `<root>/""/<stamp>`.
`paths.resolve_data()` passes absolute paths through (that is how the user's resume,
which lives outside the plugin, is addressed) and anchors relative ones under DATA.

**DATA is derived from ROOT, and the derivation outranks `CLAUDE_PLUGIN_DATA`** —
`hireshire/plugin_dirs.py` owns this. Claude Code resolves that variable from the
plugin *identifier*, which differs by interface: `cli` and `claude-vscode` report
`hireshire@hireshire`, `claude-desktop` reports an inline source and yields
`hireshire-inline`. Following it splits one install's state across two directories.
ROOT does not have the problem, and the derivation drops the version segment, which
is what carries the user's database across an update.

The third root belongs to the user, not the plugin. **WORKSPACE** is the folder they
made for their job search — resume in `resume/original/`, and inside
`hireshire_run_results/` one directory per **calendar day**, each holding that day's
run directories (`<YYYY-MM-DD>/<stamp>/`). Its absolute path is captured **once** by
`/hireshire:setup` into `scraper.workspace_dir`; `paths.results_root()` is the only
reader. This does not weaken the cwd rule above, it is what makes obeying it
possible: the *skill* knows the working directory and records it, the engine only
ever reads config, so a session launched from another folder still writes to the
folder the user chose. `hireshire/workspace.py` owns creating it and copying the
resume in, and refuses a workspace inside ROOT or DATA. Empty `workspace_dir` falls
back to `DATA/results`, which is where installs predating the setting keep writing —
so every statement about the results path needs that clause.


