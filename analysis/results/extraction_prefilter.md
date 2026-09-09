# Structured extraction as a deterministic pre-filter — spike result

Run against `2026-09-08T05-51-47Z` (213 descriptions with a `matches` row), via
`analysis/extraction_spike.py`, model `claude-haiku-4-5`. The extraction cache is
gitignored, so the numbers are recorded here.

**Verdict: it works, and it earns its place on precision. It does not save money.**

## 1. Extraction reliability

| | |
|---|---|
| descriptions attempted | 213 |
| parsed cleanly into the schema | **213 (100%)** |
| stated an explicit `min_yoe` | 155 (73%) — the other 27% are `null` and are **kept** |

100% is on one model against one corpus; treat it as "no parsing problem worth
designing around yet", not as a guarantee.

## 2. Effect above `min_score: 3.0`

61 jobs clear the cross-encoder cutoff. The filter kills **18 of them (30%)** —
10 on `role_family`, 8 on years of experience.

**The highest judge score among all 18 killed is 26**, against a shortlist threshold
of 65. A 39-point margin, and far clearer than the ≥53 floor the spike was asked to
respect. The role-family kills are the cleanest of the two: every one scored 0–10.

```
cross= 5.90 judge= 26  google      Customer Engineer III, Outcome   | needs 10y vs 4y
cross= 4.40 judge=  0  perscholas  IT Support Technical Instructor  | Education
cross= 3.40 judge=  0  powerdigit  Influencer Social Media Strateg. | Marketing
cross= 3.37 judge=  0  tacnet      Talent Acquisition Specialist    | HR/Recruiting
cross= 3.16 judge=  8  alliancede  Research Fellow                  | Legal
```

Those middle three are exactly the rows that survive a 3.0 logit cutoff and cannot be
separated from real matches by *any* threshold — they are semantically tech-adjacent
and categorically wrong. That is the case for this stage in one line.

## 3. Safety floor

**PASS.** Nothing shortlisted and nothing scored ≥ 53 was killed.

Stated honestly: 213 rows carry a judge score but only **3 are shortlisted**, which
cannot support precision/recall. The claim that survives the corpus is the stronger
and simpler one above — *nothing it killed scored above 26*.

## Cost: roughly neutral, do not sell it as a saving

Placed after the cutoff, this is 61 Haiku extractions to avoid 18 Sonnet judge calls.
At roughly 1:3 pricing that is ~20 Sonnet-equivalents spent to save 18 — a wash, and
slightly negative before the precision gain. Placed *before* the cutoff (213
extractions) it is clearly worse. **After the cross-encoder is the only placement that
makes sense.**

Note also that `seen_jobs` retires a job once it has an outcome, so the usual
"cache the JSONs and amortise" argument buys almost nothing here.

## Risks to carry into any implementation

- **The resume extraction is a single point of failure.** One call produced
  `role_families: [Engineering, Data/ML, Product, Operations], total_yoe: 4`, and every
  verdict above is relative to it. Narrower families would have made the filter far
  more aggressive. This must be shown to the user at setup and confirmed, never
  silently trusted.
- **YoE is the riskier half.** All 8 kills here had gaps of 3–11 years beyond the
  candidate and the judge agreed with every one — but the tolerance (`+2`) is a guess,
  and `null` meaning *keep* is load-bearing for the 27% that state nothing.
- **A family missing from `ROLE_FAMILIES` becomes `Other`, which never matches**, so
  gaps in the vocabulary read as false drops. Widen it before trusting a kill.
- Extraction drops must go **into** `_RETRYABLE_SKIP_REASONS`. A misparse is the same
  class of failure as the `--json-schema` bug that permanently retired 100 jobs.

  **This holds only for an LLM extractor, and the YoE half no longer uses one.**
  `hireshire/funnel/experience.py` reads years with a regex, which has no transient
  failure mode: the same description and the same `candidate_years` give the same
  answer every time, so `yoe_below_requirement` is a *verdict* and stays OUT of
  `_RETRYABLE_SKIP_REASONS`, by the same argument that keeps `rerank_below_cutoff`
  out. The rule above still governs `role_family`, which is still unimplemented and
  would still need a model. See `analysis/results/yoe_gate.md`.
