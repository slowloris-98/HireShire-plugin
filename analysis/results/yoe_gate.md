# The regex years-of-experience gate — measurement

Run via `analysis/yoe_gate_eval.py` against the same corpus as
`extraction_prefilter.md` (213 descriptions carrying a `matches` row, sweep
`2026-09-08T05-51-47Z`), at the shipped defaults: `candidate_years: 4`,
`tolerance_years: 0.5`.

**Verdict: it passes the safety floor, and it costs nothing.** This is the half of
the extraction spike that no longer needs an LLM.

## 1. Parser vs the cached Haiku labels

| | |
|---|---|
| exact agreement | 160/213 (75%) |
| **read HIGHER than the label** | **0** |
| found a requirement where the label said none | 16 |
| read lower than the label | 19 |
| found none where the label had one | 18 |

**Zero over-reads is the number that matters.** Reading high is the only direction
that kills a job wrongly; reading low or reading nothing merely keeps a job the gate
could have dropped. The taking-the-lowest rule is what buys this — it was the only
aggregation of the three measured (lowest / first / highest) with no over-reads.

The 75% understates accuracy, because the labels are an LLM's output rather than
ground truth and most disagreements are the label being wrong:

```
label=None regex=15  Architect, Front-End/Client Engineering
      "…and 15+ years of industry experience in senior software engineering roles"
label=None regex= 5  Superintendent
      "At least five years of relevant experience as a Superintendent"
label=None regex= 2  Software Engineer III, Infrastructure, Infra Bigtable
      "2 years of experience with software development using C++"
```

Haiku dropped spelled-out numbers, requirements attached to a degree clause, and
anything phrased as *preferred*. The shipped parser treats "preferred" as a
requirement on purpose, so that last group is a deliberate divergence rather than an
error — see `hireshire/funnel/experience.py`.

## 2. Effect above the cutoff

61 jobs clear `min_score: 3.0`. The gate kills **19 of them (31%)**.

```
cross= 5.94 judge= 25  advancedspace  Software Engineer (5-8 yrs)     | needs 5y vs 4y
cross= 5.90 judge= 26  google         Customer Engineer III, Outcome  | needs 10y vs 4y
cross= 5.76 judge= 19  google         Customer Engineer, Platform     | needs 10y vs 4y
cross= 5.54 judge= 25  google         Content Adversarial Red Team    | needs 7y vs 4y
cross= 4.95 judge= 31  google         Customer Engineer, Outcome, Fin | needs 6y vs 4y
```

The first row is the case for the whole stage in one line: a posting whose **title
says `(5-8 yrs)`**, which the cross-encoder ranked highest of anything it killed, and
which the judge then scored 25. That job was always going to be rejected; the only
question was whether an LLM call got spent finding out.

## 3. Safety floor — PASS

Nothing shortlisted was killed, and the **best judge score among all 19 casualties is
31**, against a shortlist threshold of 65–75. A 34-point margin.

Stated honestly: 213 rows carry a judge score but only 3 are shortlisted, which
cannot support precision/recall. The claim the corpus does support is the one above —
*nothing it killed scored above 31*.

## Why this is measured separately from the spike

`extraction_prefilter.md` recorded a PASS at `YOE_TOLERANCE = 2`. **That result does
not transfer**: the shipped tolerance is 0.5, four times tighter, and kills strictly
more (19 here against the spike's 8). The floor had to be re-run, and any future
change to `tolerance_years` invalidates it again. That is what `yoe_gate_eval.py` is
for — re-run it rather than reasoning about the number.

## Cost

Zero. Both earlier framings — "cache the JSONs and amortise", and the spike's
~20-Sonnet-equivalents-to-save-18 wash — are moot once no model is involved. The
regex runs once per *cluster*, not per posting, and only on jobs that already cleared
the cross-encoder cutoff.

## Carried into the implementation

- **The drop is a verdict, so it retires the job.** Deterministic in, deterministic
  out. This reverses `extraction_prefilter.md`'s guidance, which was written about an
  LLM extractor where a misparse is a transient failure. See the note there.
- **`candidate_years` is the single point of failure**, exactly as the spike warned
  about its resume extraction. Setup proposes a number from the resume and makes the
  user confirm it; it is never written unseen.
- **`yoe_required` is recorded on every reranked row even when the gate is off**, so
  the question "what would enabling this cost me" can be answered from a real run
  instead of re-run offline.
