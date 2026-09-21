# HireShire Plugin

Automated job search on your own Claude subscription.

## 1. Prerequisites

- A Claude subscription (to score jobs on a ChatGPT plan instead, also install the
  Codex CLI and run `codex login`; setup offers it)
- An optimized, refined resume, in a new folder
- ~3 GB of free disk space

## 2. Install and set up

1. Start a Claude Code session from that folder.
2. Install the plugin and setup:
   ```
   /plugin marketplace add slowloris-98/HireShire-plugin
   ```

   ```
   /plugin install hireshire@hireshire 
   ```
   (select install for you)

   ```
   /hireshire:setup
   ```

   `setup` will ask your preferences for the run (~15 min).

## 3. Run HireShire

Start Hireshire from a claude session in a folder with your resume.

```
/hireshire:start-orchestration
```

The sweep's status and your shortlisted jobs are here:

**Windows**
```
All sweeps:  C:\Users\<you>\<job-search-folder>\hireshire_run_results\Dashboard_Lifetime.html
One sweep:   C:\Users\<you>\<job-search-folder>\hireshire_run_results\<YYYY-MM-DD_HHMMSS>\Dashboard_<YYYY-MM-DD_HHMMSS>.html
```

**macOS**
```
All sweeps:  ~/<job-search-folder>/hireshire_run_results/Dashboard_Lifetime.html
One sweep:   ~/<job-search-folder>/hireshire_run_results/<YYYY-MM-DD_HHMMSS>/Dashboard_<YYYY-MM-DD_HHMMSS>.html
```

Full details: [docs/SPECS.md](docs/SPECS.md)

## 4. System architecture

![HireShire system architecture](docs/HLD.png)
