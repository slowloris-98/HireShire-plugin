from __future__ import annotations

import logging

from hireshire.funnel.config import FunnelConfig
from hireshire.funnel.detail_fetcher import DetailFetcher
from hireshire.funnel.relevance import EncoderRelevance
from hireshire.matcher.config import TitleFilterConfig
from hireshire.matcher.scorer import MatchResult
from hireshire.matcher.title_filter import filtered_result
from hireshire.models.job import Job

logger = logging.getLogger(__name__)


class Funnel:
    """Matcher-entry relevance gate. Drop-in for `apply_title_filter`: `process`
    returns `(to_score, filtered_results)` with the same shape.

    Stages, in order:
      1. code exclude filter        → drop as "title_excluded"
      2. code include fast-pass     → keep (cheap, and immune to encoder mistuning)
      3. encoder relevance          → keep if cos-sim >= threshold, else "title_low_relevance"
      4. detail hydration           → fetch content_text for surviving list-only
                                      Workday/BambooHR jobs

    Everything here operates on the TITLE, which is all that exists for list-only
    boards before hydration. That makes it a cheap recall net, not a verdict: the
    real precision decision is the cross-encoder rerank the matcher runs over the
    hydrated descriptions once the whole sweep is in (see funnel/rerank.py). Keep
    the encoder threshold low accordingly.

    Use as an async context manager so the detail-fetch http client is opened once
    for the whole matcher run."""

    def __init__(self, funnel_cfg: FunnelConfig, title_cfg: TitleFilterConfig, run_id: str):
        self._cfg = funnel_cfg
        self._title_cfg = title_cfg
        self._run_id = run_id
        self._relevance = EncoderRelevance(funnel_cfg.encoder)
        self._detail = DetailFetcher(funnel_cfg.detail_fetch)

    async def __aenter__(self) -> "Funnel":
        await self._detail.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._detail.__aexit__(*exc)

    async def process(self, jobs: list[Job]) -> tuple[list[Job], list[MatchResult]]:
        excludes = [kw.lower() for kw in self._title_cfg.exclude_keywords]
        includes = [kw.lower() for kw in self._title_cfg.include_keywords]

        filtered: list[MatchResult] = []
        passed: list[Job] = []       # kept so far (include fast-pass + encoder survivors)
        candidates: list[Job] = []   # not excluded, not fast-passed — go to the encoder

        for job in jobs:
            title_lower = job.title.lower()
            if any(kw in title_lower for kw in excludes):
                filtered.append(filtered_result(job, "title_excluded", self._run_id))
            elif includes and any(kw in title_lower for kw in includes):
                passed.append(job)
            else:
                candidates.append(job)

        # --- Relevance stage ---
        if self._cfg.encoder.targets:
            mask = await self._relevance.relevant_mask([j.title for j in candidates])
            for job, ok in zip(candidates, mask):
                if ok:
                    passed.append(job)
                else:
                    filtered.append(filtered_result(job, "title_low_relevance", self._run_id))
        else:
            # No encoder targets configured → fall back to the classic include rule so
            # behaviour matches the pure code title filter.
            for job in candidates:
                if includes:
                    filtered.append(filtered_result(job, "title_no_include_match", self._run_id))
                else:
                    passed.append(job)

        # --- Detail hydration for list-only Workday/BambooHR survivors ---
        hydrated = await self._detail.hydrate(passed)
        return hydrated, filtered
