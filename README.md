# HireShire Plugin

Automated job search on your own Claude subscription.

## 1. Prerequisites

- A Claude subscription
- An optimized, refined resume, in a new folder
- ~3 GB of free disk space

## 2. Install and set up

1. Start a Claude Code session from that folder.
2. Install the plugin:
   ```
   /plugin marketplace add slowloris-98/HireShire-plugin
   /plugin install hireshire@hireshire
   ```
3. Run `/hireshire:setup` and answer the questions as asked (~15 min).

## 3. Run HireShire

```
/hireshire:start-orchestration
```

The sweep's status and your shortlisted jobs are here:

**Windows**
```
All sweeps:  C:\Users\<you>\<job-search-folder>\hireshire_run_results\overview.html
One sweep:   C:\Users\<you>\<job-search-folder>\hireshire_run_results\<YYYY-MM-DD_HHMMSS>\<YYYY-MM-DD_HHMMSS>_overview.html
```

**macOS**
```
All sweeps:  ~/<job-search-folder>/hireshire_run_results/overview.html
One sweep:   ~/<job-search-folder>/hireshire_run_results/<YYYY-MM-DD_HHMMSS>/<YYYY-MM-DD_HHMMSS>_overview.html
```

Full details: [docs/SPECS.md](docs/SPECS.md)

## 4. System architecture

![HireShire system architecture](docs/HLD.png)
