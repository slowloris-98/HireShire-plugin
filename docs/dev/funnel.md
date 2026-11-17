# The funnel and the judge

> Developer notes, moved verbatim from the root `CLAUDE.md`. Not loaded every
> session: the root file points here, and the nested `CLAUDE.md` beside the code
> imports it when Claude works in that directory.

### The funnel is the interesting part

Scoring every posting with an LLM is what makes a 15,000-employer sweep accurate,
and also what makes it expensive. The pipeline spends that budget deliberately:

```
location + age      free
exclude keywords    free                          funnel.py:55
bi-encoder          cheap, TITLE only             relevance.py — a recall net
detail hydration    only for DETAIL_SOURCES       detail_fetcher.py:20
cross-encoder 68m   full description → logit      rerank.py
cluster             one call per requisition      cluster.py — needs those logits
min_score cutoff    per job, per batch → LLM      matcher.py:_process_batch
years-of-experience free, regex, per cluster      experience.py — after the cutoff
top_k               a fuse on calls, not a gate   matcher.py:_CallBudget
```

**Every stage is a per-job decision, and that is the design.** Selection used to be a
global top-K over the pooled sweep, which meant no job could be judged until every
job had been seen — the queue architecture bought nothing for the part the user waits
on. A cutoff is a statement about one job against one profile, so the whole pipeline
now streams: scrape → gate → hydrate → cluster → rerank → judge, one employer at a
time. Applying stays a separate phase; see the note in `orchestrate.py`.

Five things follow that are easy to break:

- **Title keywords match whole words, and the boundaries are conditional.** This
  reverses a plain `kw in title_lower` and must not be restored: a `title_excluded`
  drop is a verdict, so `intern` retired Internal Tools Developer *permanently*, `ios`
  retired Kiosk Manager and `mobile` retired Automobile Design Engineer. The rule lives
  once in `title_filter.title_matches`; `funnel.py` and `apply_title_filter` both call
  it. Two things it is easy to get wrong. Keywords are **phrases with punctuation**
  (`"manager, engineering"`, `"sr. "`, `"full-stack"`), so it escapes the keyword
  rather than tokenising — a `\b\w+\b` splitter cannot express them. And the boundary
  is a lookaround applied **only on a side whose character is a word char**: `\b`
  asserts a *transition*, so a trailing one on `"sr."` would demand the very word
  character the keyword stops at, and the match would never fire. Leading/trailing
  whitespace is stripped for the same reason. Nothing is stemmed, in either direction
  — `intern` misses Interns, `internship` misses Intern — which was chosen over a
  suffix allowance because the gate is permanent and a morphology guess is not
  reviewable. A blank keyword matches nothing; unguarded it would empty the sweep.

  Setup's drafting rules were reversed to match, and the reversal should not be undone.
  It now writes **bare single words scoped to the user's profession** — `staff`, not
  `staff engineer` — because a phrase catches the one specialisation the model thought
  of and misses Staff ML Engineer, Staff Data Scientist and the rest. The old rule
  qualified every rung word with a field noun; that was substring damage control, and
  restoring it as a safety measure costs recall and buys nothing. What keeps a bare
  word safe is the **profession check**, not the noun: `staff` is a promotion in
  software and the job itself for a Staff Nurse, so the field decides whether the word
  is drafted at all. Because nothing is stemmed, setup also **expands each term into
  every spelling employers write** (`intern, interns, internship, internships`;
  `senior, sr`; `vice president, vp`) — and `sr.` is never drafted, since bare `sr`
  already covers `Sr.` through the non-word-character rule above.
- **Only the cutoff costs money.** Both gates before it run locally. Tightening the
  bi-encoder buys CPU seconds and skipped detail fetches, never LLM calls, and pays
  for them in recall at the *title-only* stage. This is the single most common wrong
  instinct about this funnel.
- **The bi-encoder threshold is deliberately low (0.30)** and does not transfer
  between users. Outside tech, titles are branded and generic (Account Manager,
  Client Partner, Growth Partner), so their cosines bunch into a narrow band and a
  threshold tuned on engineering titles passes everything or nothing. Note also that
  max-over-targets rises with the number of anchors, so a longer `targets` list
  loosens the gate further on its own.
- **`min_score` is a raw logit and is personal.** Not a probability, not comparable
  between users, and void the moment `rerank.model` changes. `scripts/calibrate_cutoffs.py`
  derives it from the user's own scored postings; the shipped 3.0 is a starting point
  borrowed from this project's own analysis corpus (where it admits ~61 jobs a sweep,
  inside `top_k`), not a value tuned to any particular user. It sits deliberately well
  above 0.0 — the models' own decision boundary, which shipped through 0.2.x and was
  permissive enough that the budget rather than the cutoff usually decided what got
  scored.
- **Clustering still works per batch, and this is load-bearing.** Postings group by
  `board_token` plus description, and the scraper emits one employer per queue item,
  so every member of a cluster is in the same batch by construction. That is what let
  top-K go without 31 copies of a requisition becoming 31 LLM calls. Clustering must
  run *after* the reranker: `cluster.group` anchors each cluster on its best-scoring
  member, so swapping the two lines would let scrape order pick the representative.
- **Greenhouse/Ashby/Lever ship the description in the list response**
  (`greenhouse.py:89`, `ashby.py:54`, `lever.py:59`). Only Workday, BambooHR and the
  direct portals need hydration. That is why full-text reranking is free on the
  default board set, and why the title-only gates exist at all.

The rerank cascade is gone. A 17m model used to read everything and the 68m re-read
the top `refine.depth`, which existed solely to make a global top-K affordable — and
cost a permanent hazard, since the two stages emitted incomparable logit scales that
any naive sort silently mixed. The 68m model now reads every candidate, spread across
the sweep rather than run in one block, and there is one scale. `rerank_score_wide`
survives as a **read-only** column: rows written before the collapse carry it, the
reports render them, and nothing writes it any more. The single-stage reranker that
preceded all of this failed silently for a whole run (correlation with the eventual
LLM score: **+0.16**), which is why `RerankConfig` documents its scale so heavily.

Note also why `max_doc_chars` is generous now: the old 1,200-char cap truncated
**41% of descriptions before their first requirements heading** (median heading
offset: character 1,094), so the cross-encoder was scoring company boilerplate.
Descriptions tokenise at ~5.06 chars/token and the longest measured was 2,693
tokens, so against an 8,192-token window the setting is a cost dial, not a limit.

### Two invariants with teeth

- **A job may be retired on a verdict, never on a deferral or an error**, and the two
  rerank drops sit on opposite sides of that line. `llm_call_cap_reached` is a
  deferral — the run ran out of calls, and the job may be the best thing in a quieter
  sweep — so it stays in `_RETRYABLE_SKIP_REASONS`. `rerank_below_cutoff` is a
  verdict and is deliberately absent: same profile, same model, same description
  means the same logit, so with `max_age_hours: 24` and a 4-hour poll, retrying it
  would re-run one deterministic computation ~6 times a day to reach the same answer,
  and it has nothing to win against. Getting this backwards in either direction is a
  real bug: one wastes the funnel, the other permanently discards a job that was only
  unlucky. `rerank_below_top_k` stays listed as retryable for rows written before the
  split.
- **The years-of-experience gate is a regex, and that is not a compromise.**
  `hireshire/funnel/experience.py` reads a stated minimum out of the description with
  no model at all. An encoder cannot do this — a bi-encoder pools to one vector and a
  cross-encoder to one logit, so neither has a span output, and pooled embeddings are
  bad at magnitude anyway ("2+ years" sits near "12+ years"). An LLM can, and
  `analysis/extraction_spike.py` measured Haiku doing it, but the cost was a wash:
  ~20 Sonnet-equivalents to save 18 judge calls. It clears the safety floor that
  licenses it: on sweep `2026-09-09T06-51-12Z` the best judge score among its
  casualties was 44 against a shortlist threshold of 65, and nothing shortlisted was
  killed (`analysis/results/yoe_gate.md`, whose section 1 predates the current
  aggregation — read the banner there).

  Five rules hold it together, and each reverses an instinct:

  - **The `+` is the requirement marker, not the word "experience".** The parser used
    to require experience language within a short window of the number, which threw
    away every posting that writes the domain instead — `7+ years owning financial
    planning`, `10+ years in software engineering`, `5+ years of production support`.
    An **open-ended minimum** (`5+ years`, `5 plus years`, `at least 5 years`,
    `minimum of 5 years`) is a requirement whatever follows it, and company prose
    almost never uses that form. Widening the word list instead was tried and
    rejected: a rule loose enough to admit those lines also read "For 20 years, Acme
    has been building homes" as a 20-year requirement. Three tiers, first non-empty
    wins — open-ended, then range (lower bound), then the old bare-count-plus-
    proximity path, which is retained because dropping it costs 7 of the 104
    reachable labels (91% → 85%) and takes missed requirements from 1 to 10.
  - **Highest open-ended minimum governs.** A posting listing `8+ / 12+ / 3+` compares
    against **12**. This reverses the original lowest-governs rule and the reversal is
    deliberate: a candidate who cannot clear the highest bar a posting names is not
    getting the job, and the lowest reading sent 6 of every 50 paid calls to postings
    needing 5-8 years. The label corpus in `analysis/cache/extraction.json` encodes
    lowest-governs, so the parser now reads *higher* than a label occasionally by
    design — do not "fix" that. What licenses it is the safety floor, not agreement:
    on sweep `2026-09-09T06-51-12Z` the change dropped 13 of 50 paid calls (26%), the
    best judge score among them was 44 against a shortlist threshold of 65, and
    nothing shortlisted was touched. Agreement on the labels rose 81% → 91% anyway.
    Note `analysis/yoe_gate_eval.py` section 1 therefore measures policy divergence,
    not error, and its sample is thin — see the caveats in its docstring.
  - **"Preferred" is treated exactly like "required"**, which is where the spike's
    prompt differed and why it returned null on most preference-phrased postings.
  - **It gates only from below.** `5-10 years` is read as *at least 5*. Ranges are
    matched *before* the open-ended tier and their spans excluded from it, so the
    upper bound cannot look like a second requirement — without that ordering
    `3 to 7+ years` would read as an open-ended 7 rather than a range from 3.
    Nothing is dropped for being over-qualified.
  - **It runs after the `min_score` cutoff, not before it.** Both are LLM-free, so
    the ordering buys two other things: `stages["above_cutoff"]` keeps meaning what
    the overview page says it means, and a wrong `candidate_years` can only touch
    jobs that were about to cost a call. YoE drops are counted into `above_cutoff`
    for the same reason cap drops are.

    **The overview page deliberately disagrees, and both are right.** Its `Relevant
    jobs` tile (`Database._relevant_sql`) excludes `yoe_below_requirement`, because
    that tile answers "what survived every free gate" — a user reading it wants the
    jobs still in the running, and a posting the resume cannot qualify for is not one.
    `stages["above_cutoff"]` answers "what reached the point of costing money", which
    is a question about the funnel, not about the user. So the tile and `matcher.py`'s
    `above cutoff → judged` console line will differ by exactly the YoE count. Do not
    "fix" either to match the other.

  `yoe_below_requirement` is a **verdict** and stays out of `_RETRYABLE_SKIP_REASONS`
  — same description, same `candidate_years`, same answer. This reverses
  `analysis/results/extraction_prefilter.md`, which required extraction drops to be
  retryable; that was written about an LLM extractor, where a misparse is transient.
  `yoe_required` is recorded on every reranked row even when the gate is switched
  off, so "what would enabling this cost me" is answerable from a real run.

- **The search profile never reaches the scoring prompt.** It states transferable
  and inferred framing ("React → component-based UI development"). It is the
  reranker's query only. A judge reading it would credit the candidate for skills
  the resume does not evidence. Keep it out of `projects_path`, which *is*
  concatenated into the prompt at `scorer.py`.

- **Duplicate requisitions are grouped, never dropped.** `cluster.py` keys on
  `(board_token, description)` — **titles are deliberately never compared.** One
  representative is scored and the verdict is copied to every sibling, so all 31
  copies keep their own location and link in the results CSV. Siblings carry
  `duplicate_of_cluster`, which must stay **out** of `_RETRYABLE_SKIP_REASONS`: they
  have been judged, just by proxy. Members of a cluster that misses the cut inherit
  the representative's drop reason, so their retryability follows the rule above.

  This reverses the original title-based key, and the reversal was forced by data —
  do not "restore" it. An audit of 192,700 real postings found that of 2,578 clusters
  formed by stripping a trailing parenthetical, **1,739 grouped postings whose
  descriptions differed**. SpaceX qualifies titles by programme, so one cluster held
  seven unrelated jobs (`Automation & Controls Engineer` for Facilities, Raptor,
  Starlink, Starship…); the same strip collapsed shifts, employment types and ladder
  levels (`(L1)` with `(L3)`). Six of seven then inherited a non-retryable reason and
  were retired permanently on a verdict about a different job.

  Exact description equality is too strict — employers interpolate pay bands and
  addresses per market — so `dedupe.max_word_diff` (default 10) allows a small drift.
  It is a **multiset symmetric difference, where a substitution costs 2**; every
  calibration is in those units. Two costs are accepted and should not be "fixed"
  without new data: templated employers over-merge (sweetgreen ships identical text
  for `Assistant Coach` and `Assistant Restaurant Manager`), and localised employers
  fragment (~+900 to +1,010 LLM calls per sweep, which land on the retryable cap).


## The codex judge

- **The `codex` judge is `codex exec`, and five things about it were learned by
  probing, not read.** `hireshire/codex_cli.py` holds the rules; `CodexBackend` in
  `scorer.py` applies them. Each fails silently if undone:
  - `--output-schema` takes a **path**, and the schema must be OpenAI's strict
    dialect (`codex_cli.strict_schema`) or every call is an HTTP 400.
  - The answer is the **last** `agent_message` (openai/codex#19816). An
    `item.completed` of type `error` is routine and is not a failure.
  - The judge is **not an agent**. Tools, skills, sub-agents and environment context
    are stripped, which took one call from 11,207 input tokens to 1,769, and tools
    left on can make Codex drop the schema (#15451). `--disable` names are filtered
    through `codex features list`, since an unknown one fails the call.
  - **No caching between calls, by design of the CLI.** `prompt_cache_key` comes
    from the thread id and every exec is a new thread (#21796, open). Do not resume
    one thread to "fix" it — the context would grow by a posting per job. The
    tally's `caches=False` keeps the no-cache warning from blaming the prompt.
  - OpenAI counts cached tokens *inside* `input_tokens`, and there is no price, so
    `cost_usd` is None rather than 0.
  Neither `matcher.model` nor `applier.model` has a Codex default: setup pins one from
  `setup_cli.py codex-check`, and both refuse a Claude name through the shared
  `codex_cli.is_claude_model`, because `provider` alone can be switched. The applier
  reaches `codex exec` with its own rules — see `docs/dev/applier.md`, and note its
  sandbox and approval settings are **not** the judge's.

