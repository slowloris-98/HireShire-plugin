# Funnel configuration evaluation

`funnel_eval.ipynb` replays one real sweep through eight ranking configurations and
writes out what each would have handed the LLM scorer, plus what each cost. No LLM
scoring happens anywhere — the notebook stops exactly where scoring would begin.

It answers one question: **does the cheap cascade pick the same jobs the expensive
model would have, and what does the difference cost?** It cannot answer "which
config is accurate", because the corpus has no labels (see *Caveats*).

## Setup

The notebook needs a very specific environment, and the reason is worth reading
before you substitute your own.

The Ettin rerankers are module-list CrossEncoders whose scoring head lives in
`2_Dense`/`4_Dense`. Under `transformers < 5` the loader silently falls back to
`ModernBertForSequenceClassification` and **randomly initializes that head** — every
score becomes noise, with nothing but a warning to say so. Meanwhile the plugin's
own venv ships `torch+cpu`, and a full pass on CPU takes hours.

So: a venv that inherits the system CUDA torch and shadows only the two packages
that matter. `--system-site-packages` is what avoids a 2.5 GB torch download.

```sh
# from the repo root
"C:/Users/atrey/AppData/Local/Programs/Python/Python312/python.exe" \
    -m venv --system-site-packages analysis/venv
./analysis/venv/Scripts/python.exe -m pip install -U \
    "transformers>=5.15" "sentence-transformers>=5.7" ipykernel nbclient
./analysis/venv/Scripts/python.exe -m ipykernel install --user \
    --name hireshire-funnel-eval --display-name "HireShire funnel eval"
```

Pip will warn about `crewai`, `langchain-huggingface` and `streamlit` conflicting.
Those are unrelated system packages the notebook never imports; the warning is
expected.

Cell 0 asserts CUDA availability and `transformers >= 5`, and every model load
asserts that a relevant job outscores an irrelevant one by more than 3 logits. A
randomly-initialized head fails that instantly.

## Running

Open `funnel_eval.ipynb` with the **HireShire funnel eval** kernel and run all.
Headless:

```sh
./analysis/venv/Scripts/python.exe -m jupyter nbconvert --to notebook \
    --execute --inplace --ExecutePreprocessor.timeout=7200 analysis/funnel_eval.ipynb
```

Set `FUNNEL_SAMPLE_N=400` for a ~3 minute smoke pass that exercises every cell.
Smoke results are for plumbing only — with a pool that small, `top_k=100` selects a
quarter of it and every agreement metric inflates.

First full run is ~30 minutes of GPU compute. Scores are cached to `analysis/cache/`
keyed on model, pool, profile and the SHA of `rerank.py`/`cluster.py`, so re-runs are
near-instant and any edit to the ranking code correctly invalidates them. Timings are
recorded **only on a cache miss**, so the reported cost stays a real measurement.

The 32m reranker is not in the plugin and downloads on first run (~130 MB).

## Editing

The notebook is generated: cell sources live in `build_notebook.py` as ordinary
Python strings rather than hand-escaped JSON. Edit that and re-run it:

```sh
./analysis/venv/Scripts/python.exe analysis/build_notebook.py
```

## Data

Read-only, from the original HireShire repo's database
(`D:\Atreya\College\Projects\HireShire\data\hireshire.db`, 11.3 GB). The connection
is opened `mode=ro` with `PRAGMA query_only=ON`, and the notebook *proves* it cannot
write before touching real data. Nothing is ever written back.

Run `2026-07-21T17-43-52Z` — 20,722 jobs, the largest in the database — restricted
to the 13,281 with real descriptions. The 7,441 title-only Workday/BambooHR rows
cannot be hydrated offline, and dropping them also matches the plugin's defaults,
where those two board types ship disabled.

## Outputs (`results/`)

| File | Contents |
|---|---|
| `selected__<config>.csv` | the top-100 clusters that config would send to the LLM |
| `ranked__<config>.csv` | every cluster representative, in rank order |
| `summary.csv` | per-config pool sizes, pair counts, per-stage wall-clock |
| `m1_depth_sweep.csv` | is `refine.depth: 500` deep enough? |
| `m2_recall_vs_reference.csv` | agreement with `direct-68m` |
| `m4_jaccard_top100.csv`, `m4_rbo.csv` | pairwise agreement matrices |
| `m5_selection_health.csv` | company concentration, length bias, title-cosine sanity |
| `human_review.csv` | union of every config's top 20, with a blank `verdict` column |

## Caveats

**Agreement is not accuracy.** `direct-68m` is the reference because it is the
strongest, least-truncated configuration available — not because it is known to be
right. `human_review.csv` exists to break that circularity: marking its rows turns
every agreement number into a real precision@20.

**The seconds are GPU fp16.** Production runs fp32 on CPU, where `CHANGELOG.md`
measured stage 1 at ~32 min for a 7,021-job sweep. These numbers are a cost *model*,
consistent across configs, not production sweep latency.

**`max_doc_chars` and `max_length` are inert here.** After HTML stripping almost
nothing on this corpus exceeds 15,000 chars, and the longest pair is well inside
4,096 tokens. They are held at production values for parity, but no result turns on
them.

**One run, one persona.** A config that wins here has won once.
