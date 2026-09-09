# System architecture

How a job gets from a company's careers page to your shortlist.

See the [README](../README.md) for install and settings, and `CLAUDE.md` for the
file-level internals.

## The whole flow

Everything runs on your machine except scoring. Your resume never leaves your disk;
only the jobs that survive the funnel are sent to Claude, on your own subscription.

```mermaid
flowchart TD
    subgraph local["On your machine"]
        direction TB
        R["Your resume"]
        SET["/hireshire:setup<br/>writes your profile and settings"]
        SWEEP["Sweep<br/>one-off, or every few hours"]
        B1["Greenhouse, Lever, Ashby, direct<br/>15,868 employers"]
        B2["Workday, BambooHR<br/>24,200 more, off by default"]
        FUN["Relevance funnel<br/>free, see below"]
        OUT["Shortlist CSV, reasoning report,<br/>dashboard, local database"]
        APP{"enable_applier?"}
        YOU["You apply yourself"]

        R --> SET --> SWEEP
        SWEEP --> B1 --> FUN
        SWEEP -.-> B2 -.-> FUN
        OUT --> APP
        APP -->|"off by default"| YOU
    end

    subgraph away["Leaves your machine"]
        direction TB
        CLAUDE["Claude scores each survivor 0-100<br/>your Pro or Max subscription"]
        EMP["Employer's application form"]
    end

    FUN -->|"survivors only"| CLAUDE
    CLAUDE --> OUT
    APP -->|"on: a browser fills and submits"| EMP

    classDef off fill:#f5f5f5,stroke:#9e9e9e,color:#424242,stroke-dasharray:4 3
    classDef away fill:#fff4e5,stroke:#e69100,color:#7a4f00
    class B2 off
    class CLAUDE,EMP away
    style local fill:#fbfcfe,stroke:#c8d3e3,color:#123a7a
    style away fill:#fffaf2,stroke:#e8c48a,color:#7a4f00
```

## The funnel

Blue stages run locally and cost nothing; only the orange one spends quota. That is
why `min_score`, the gate directly in front of it, is the lever that controls what a
sweep costs — tightening the title gate saves CPU, not calls.

```mermaid
flowchart TD
    IN["Every posting on every board"]
    F1["Location and posting age"]
    F2["Title has no excluded keyword"]
    F3["Title similarity vs your profile<br/>encoder_threshold 0.30"]
    F4["Fetch the full description<br/>Workday, BambooHR and direct only"]
    F5["Read the whole description<br/>against your profile"]
    F6["Group duplicate postings<br/>one score per requisition"]
    CUT{"Reaches min_score 3.0?"}
    BUD{"Calls left in the budget?<br/>top_k 150"}
    LLM["Claude scores it 0-100"]
    TH{"Reaches threshold 65?"}
    SHORT["Shortlisted"]
    REJ["Scored, not a match"]
    DROP["Dropped for good"]
    DEFER["Retried next sweep"]

    IN --> F1 --> F2 --> F3 --> F4 --> F5 --> F6 --> CUT
    CUT -->|no| DROP
    CUT -->|yes| BUD
    BUD -->|no| DEFER
    BUD -->|yes| LLM --> TH
    TH -->|yes| SHORT
    TH -->|no| REJ

    classDef free fill:#eef4ff,stroke:#4571c4,color:#123a7a
    classDef paid fill:#fff4e5,stroke:#e69100,color:#7a4f00
    classDef gone fill:#fbeaea,stroke:#c0504d,color:#7a2320
    classDef back fill:#f5f5f5,stroke:#9e9e9e,color:#424242
    class F1,F2,F3,F4,F5,F6 free
    class LLM,SHORT paid
    class DROP,REJ gone
    class DEFER back
```

**The two drops are not the same.** Below `min_score` is a verdict — the same job,
profile and model give the same answer every time, so it is retired and will not come
back. Out of budget is a deferral — nothing was decided, so it returns next sweep.

## Tuning

| Setting | Default | Raise it to |
|---|---:|---|
| `encoder_threshold` | 0.30 | skip more titles before any description is fetched |
| `min_score` | 3.0 | send fewer, better-matched jobs for scoring |
| `top_k` | 150 | allow more scoring calls per sweep |
| `threshold` | 65 | shortlist only stronger matches |

An empty shortlist with a large "reached the reranker" count in the run summary means
`min_score` is too high for your resume, not that there were no good jobs.
