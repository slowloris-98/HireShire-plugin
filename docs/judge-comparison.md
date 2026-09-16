# Judge comparison

10 live postings, scored once each; accuracy = agreement with an Opus 5 (effort `high`) referee using its own neutral prompt. Old = `HEAD` judge, Sonnet 5, effort `medium`. New = checklist + bands, Sonnet 5, effort `low`, context stripped. Nano = old judge's prompt on `gpt-5-nano`.

| | Old | New | Nano |
|---|---|---|---|
| Mean abs. error vs referee | 17.9 | 25.5 | 47.7 |
| Rank correlation vs referee | 0.57 | 0.31 | 0.80 |
| Shortlist call matches referee (≥75) | 9/10 | 8/10 | 5/10 |
| Cost per call | $0.156 (list) | $0.044 (list) | $0.0021 (billed) |
| Tokens per call (in / out) | 66,918 / 2,563 | 17,815 / 2,271 | 2,720 / 5,090 |
| 5-hour window, 10 calls | 7% | 3% | none (API) |

| Job | Referee | Old | New | Nano |
|---|---|---|---|---|
| intuit: Summer 2027: Software Engineering Intern - Cyber | 15 | 70 | 80 | 90 |
| google: Software Engineer, Agent Cloud Rate Limiting | 70 | 79 | 60 | 100 |
| google: Research Engineer, GenAI, Info Task, DeepMind | 60 | 53 | 80 | 100 |
| accenturefederalservices: Full Stack Developer | 18 | 54 | 56 | 74 |
| google: Software Engineer III, Infrastructure, YouTube | 45 | 48 | 44 | 80 |
| apple: Software Engineer, OS and System Services | 38 | 35 | 40 | 72 |
| intuit: VP, Product Partnerships, BD & Partner Managemen | 3 | 6 | 16 | 40 |
| google: Software Engineer, Semantic Understanding, Searc | 40 | 60 | 56 | 100 |
| datadog: Manager I, Engineering - Applied AI - Natural La | 18 | 39 | 68 | 72 |
| google: Staff Research Engineer, Applied AI, DeepMind | 12 | 34 | 52 | 68 |

- **New is ~3.5× cheaper but less accurate here.** Almost all the saving is input (context stripping); output barely moved, though thinking fell from 787 to 59 tokens per call.
- **Both Sonnet judges miss hard eligibility blockers the referee caught:** an internship needing graduation after 2027, a TS/SCI clearance, a people-manager role, 8+ years for Staff. New over-scores these more (Datadog manager 68 vs 39). The Staff role would be dropped by the YoE gate before judging in production.
- **Nano ranks well (ρ 0.80) but inflates every score** (three 100s), so its threshold would need recalibrating. Caveats: the referee is an opinion; one run per job; the window meter moves in 1% steps, and Old's 7% includes a brief overlapping chat turn.
