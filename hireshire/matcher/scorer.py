from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from typing import Literal, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator
from tenacity import retry, retry_if_exception, stop_never

from hireshire import claude_cli
from hireshire.matcher.config import MatcherSettings
from hireshire.models.job import Job
from hireshire.matcher.prompts import SCORER_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = SCORER_SYSTEM_PROMPT

# `skip_reason` values that mean "the scorer broke", not "this job was judged". They
# live here beside MatchResult because that is where the vocabulary is defined, and
# because both matcher.py (which must not retire these jobs) and matcher/seen.py
# (which releases ones already retired) need them without importing each other.
SCORING_ERROR_SKIP_REASONS = frozenset({"api_error", "unexpected_error", "backend_unavailable"})


class UsageTally:
    """What a run's scoring actually cost, accumulated across calls.

    Scoring draws on the same allowance as the user's own Claude chat — a 5-hour
    rolling window plus a weekly one — so "how much did that sweep cost me" is a fair
    question with, until now, no answer anywhere in the product. The `-p` JSON
    envelope carries the numbers; this keeps them.

    `cache_read` is the one to watch. The resume and rubric are identical on every
    call of a run, so from the second judged job onward it should dominate `input`.
    If it stays at zero, the cached prefix is not byte-identical and the run is paying
    full price to re-read the same resume 150 times.

    `cost_usd` is the CLI's own client-side estimate, computed from token counts at
    list price. On a subscription it is not a bill — it is a relative measure, useful
    for comparing effort levels or model choices, and it must be presented that way.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.input = 0
        self.output = 0
        self.cache_read = 0
        self.cache_write = 0
        self.cost_usd = 0.0

    def record(self, envelope) -> None:
        """Add one call's usage. Silent on anything unexpected.

        Never raises and never logs a failure: this is instrumentation hanging off
        the scoring path, and a CLI version that renames a field must not be able to
        turn a working sweep into a failed one. Missing numbers show up as a zero in
        the summary, which is the correct signal that the meters are not being read.
        """
        if not isinstance(envelope, dict):
            return
        self.calls += 1
        try:
            self.cost_usd += float(envelope.get("total_cost_usd") or 0.0)
        except (TypeError, ValueError):
            pass
        usage = envelope.get("usage")
        if not isinstance(usage, dict):
            return

        def _n(*keys: str) -> int:
            for k in keys:
                v = usage.get(k)
                if isinstance(v, (int, float)):
                    return int(v)
            return 0

        self.input += _n("input_tokens")
        self.output += _n("output_tokens")
        self.cache_read += _n("cache_read_input_tokens")
        # `cache_creation` is an object split by TTL on current versions and was a
        # flat count before, so accept either shape.
        created = usage.get("cache_creation")
        if isinstance(created, dict):
            self.cache_write += sum(v for v in created.values() if isinstance(v, (int, float)))
        else:
            self.cache_write += _n("cache_creation_input_tokens", "cache_creation")

    @property
    def empty(self) -> bool:
        return self.calls == 0

    def as_dict(self) -> dict:
        """The tally as plain JSON, for `runs.stats_json`.

        Written once per run by `MatchStore.finalise`, so "what did that sweep
        cost" survives outside the log file — which, under the monitor's
        `quiet=True`, was the only copy. The dashboard no longer prints it
        (`render.SHOW_COST`): a list-price estimate on that page read as a bill.
        The row is kept regardless, because it is the only durable record.
        """
        return {
            "calls": self.calls,
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "cost_usd": self.cost_usd,
        }

    def summary(self) -> str:
        return (
            f"Scoring usage: {self.calls} calls, {self.input:,} input + "
            f"{self.output:,} output tokens, {self.cache_read:,} read from cache, "
            f"{self.cache_write:,} written to it (~${self.cost_usd:.2f} at list price, "
            f"an estimate, not a bill)"
        )


def _bounded_text(max_chars: int, description: str):
    """A string field whose bound the model is shown but a response is never failed on.

    `Field(max_length=...)` would put `maxLength` in the schema *and* reject anything
    longer, turning a 201-character rationale — from a call that has already been
    billed — into an `api_error` row. The bound is there to keep output short, which is
    a cost lever, not a correctness property, so it is advertised in the schema and
    enforced by truncation.
    """
    return Field(description=description, json_schema_extra={"maxLength": max_chars})


def _bounded_list(max_items: int, description: str):
    """A list field bounded the same way as `_bounded_text`: shown, then truncated."""
    return Field(description=description, json_schema_extra={"maxItems": max_items})


def _bounded_int(low: int, high: int, description: str):
    """An int field shown as a range and clamped into it rather than rejected."""
    return Field(description=description, json_schema_extra={"minimum": low, "maximum": high})


def _clip(value, limit: int):
    return value[:limit] if isinstance(value, (str, list)) else value


def _clamp(value, low: int, high: int):
    return max(low, min(high, value)) if isinstance(value, int) and not isinstance(value, bool) else value


# One requirement the posting states, and the resume's evidence for it.
#
# This is the checklist half of evidence-anchored scoring: the judge commits to a
# discrete, quotable decision per requirement *before* it writes any rationale or picks
# any band, so the bands are conditioned on the evidence rather than the other way
# round. The gates that act on it live in `score_bands`, in Python.
#
# Comments rather than docstrings on both wire models, deliberately: pydantic copies a
# class docstring into the JSON schema's `description`, and that schema is sent to the
# model on every judge call. Maintainer notes there cost tokens and address the wrong
# reader. `tests/test_scoring_cost.py` fails the build if a description creeps back.
class RequirementCheck(BaseModel):
    requirement: str = _bounded_text(80, "The requirement as the posting states it.")
    criterion: Literal["skills", "experience", "education"]
    mandatory: bool
    evidence: str = _bounded_text(
        120, "A verbatim quote from the resume, or an empty string when there is none."
    )
    met: int = _bounded_int(0, 2, "0 no evidence, 1 partial, 2 clearly evidenced.")

    @field_validator("requirement", mode="before")
    @classmethod
    def _clip_requirement(cls, v):
        return _clip(v, 80)

    @field_validator("evidence", mode="before")
    @classmethod
    def _clip_evidence(cls, v):
        return _clip(v, 120)

    @field_validator("met", mode="before")
    @classmethod
    def _clamp_met(cls, v):
        return _clamp(v, 0, 2)


# What the judge emits. Not what is stored — see `score_bands` and `MatchResult`.
#
# **Field order is generation order, and it is the design.** Structured output is
# produced front to back, so each `*_rationale` sits before its `*_band` and the
# checklist before both: the model reasons, then commits. This used to be the other way
# round (`core_skills_score` first), which had the judge pick a number and then write
# prose to justify it. `tests/test_scoring_cost.py` pins the order.
#
# Bands are 0-5 rather than points out of 40 because wide scales cluster on round
# numbers and align worse with human raters. `years_experience_required` is gone:
# funnel/experience.py reads that from the description for free, before this call.
class ScoringSchema(BaseModel):
    requirements: list[RequirementCheck] = _bounded_list(
        6, "At most 6 requirements the posting states, most important first."
    )
    core_skills_rationale: str = _bounded_text(200, "What matched and what was missing.")
    core_skills_band: int = _bounded_int(0, 5, "The anchored band, 0-5.")
    experience_rationale: str = _bounded_text(200, "What matched and what was missing.")
    experience_band: int = _bounded_int(0, 5, "The anchored band, 0-5.")
    education_rationale: str = _bounded_text(200, "What matched and what was missing.")
    education_band: int = _bounded_int(0, 5, "The anchored band, 0-5.")
    match_reasons: list[str] = _bounded_list(3, "At most 3 short reasons for.")
    disqualifiers: list[str] = _bounded_list(3, "At most 3 short reasons against.")
    recommend: bool

    @field_validator("requirements", "match_reasons", "disqualifiers", mode="before")
    @classmethod
    def _clip_lists(cls, v, info):
        return _clip(v, 6 if info.field_name == "requirements" else 3)

    @field_validator(
        "core_skills_rationale", "experience_rationale", "education_rationale", mode="before"
    )
    @classmethod
    def _clip_rationales(cls, v):
        return _clip(v, 200)

    @field_validator("core_skills_band", "experience_band", "education_band", mode="before")
    @classmethod
    def _clamp_bands(cls, v):
        return _clamp(v, 0, 5)


# Points each criterion is worth once stored. These are the maxima `MatchResult`, the
# overview page (`reporting.data.RUBRIC`) and every existing `matches` row are built
# on, so the bands are mapped back into them rather than the other way round — which
# is what lets the judge's scale change without the stored schema changing at all.
_CRITERION_MAX = {"skills": 40, "experience": 40, "education": 20}
_BAND_FIELD = {
    "skills": "core_skills_band",
    "experience": "experience_band",
    "education": "education_band",
}
# A criterion with a mandatory requirement the resume shows no evidence for cannot
# score above this band — the anchor the prompt itself gives band 2.
_MANDATORY_CAP_BAND = 2


def score_bands(result: ScoringSchema) -> dict:
    """Turn the judge's checklist and bands into the stored verdict.

    All the arithmetic lives here, none of it in the prompt. prompts.py's second
    editing rule asks for flat arithmetic because a wrong cap is invisible — the
    number still looks like a score. Moving it into code makes it impossible to get
    wrong rather than merely unlikely.

    Three steps, in this order:

    1. **Evidence gate.** `met: 2` with no quote is demoted to 1, and `met: 0` has any
       text in `evidence` cleared, because that text is a remark ("no MBA listed"),
       not a quote. Grounding is enforced, not requested.
    2. **Mandatory cap.** A criterion holding a mandatory item at `met: 0` is capped at
       `_MANDATORY_CAP_BAND`, **once**, however many such items it holds. The cap is
       appended to that criterion's rationale, because the overview page shows the
       rationale beside the capped number and a "strong match" over 16/40 would
       otherwise read as a bug.
    3. **Scale.** `band * max // 5` — exact on both 40 and 20, so no rounding.
    """
    checks = []
    for item in result.requirements:
        met = item.met
        evidence = item.evidence.strip()
        if met == 2 and not evidence:
            met = 1
        if met == 0:
            evidence = ""
        checks.append(item.model_copy(update={"met": met, "evidence": evidence}))

    rationale = {
        "skills": result.core_skills_rationale,
        "experience": result.experience_rationale,
        "education": result.education_rationale,
    }
    scores = {}
    for criterion, maximum in _CRITERION_MAX.items():
        band = getattr(result, _BAND_FIELD[criterion])
        missing = [
            c.requirement for c in checks
            if c.criterion == criterion and c.mandatory and c.met == 0
        ]
        if missing and band > _MANDATORY_CAP_BAND:
            band = _MANDATORY_CAP_BAND
            named = "; ".join(missing)
            rationale[criterion] = (
                f"{rationale[criterion]} [Capped: no evidence for mandatory {named}.]"
            ).strip()
        scores[criterion] = band * maximum // 5

    return {
        "relevance_score": min(100, sum(scores.values())),
        "requirements": [c.model_dump() for c in checks],
        "core_skills_score": scores["skills"],
        "core_skills_rationale": rationale["skills"],
        "experience_score": scores["experience"],
        "experience_rationale": rationale["experience"],
        "education_bonus_score": scores["education"],
        "education_rationale": rationale["education"],
        "match_reasons": result.match_reasons,
        "disqualifiers": result.disqualifiers,
        "recommend": result.recommend,
    }


class MatchResult(BaseModel):
    job_id: str
    board_token: str
    title: str
    location: str
    absolute_url: str

    relevance_score: Optional[int] = None  # None = never scored (skip_llm)

    # --- Funnel scores -------------------------------------------------------
    # Numbers on DIFFERENT SCALES. Each is ordinal within itself and none is
    # comparable to any other — a rerank logit is not a percentage. Anything that
    # sorts or displays them must keep them in separate columns.
    #
    # Bi-encoder cosine (0-1) of the TITLE against the configured target roles.
    # The number that explains a `title_low_relevance` drop; it used to be computed
    # against the threshold and thrown away.
    encoder_score: Optional[float] = None
    # Written only by runs made under the old two-model rerank cascade, where a cheap
    # pass scored everything and an accurate one re-scored the best few hundred. There
    # is one model now, so nothing writes this — it stays because the reports render
    # historical rows and a missing column would break them.
    rerank_score_wide: Optional[float] = None
    # Cross-encoder logit over the full description: the number that decided whether
    # the job was worth an LLM call. None when reranking was off or unusable.
    rerank_score: Optional[float] = None
    # Which pass produced `rerank_score`: "single" now, "wide" or "refined" on rows
    # from before the cascade was collapsed.
    rerank_stage: Optional[str] = None
    # Years of experience the POSTING asks for, read out of the description by
    # funnel/experience.py. Recorded on every reranked row whether or not the
    # experience gate is switched on, so a user can see what enabling it would cost.
    #
    # Deliberately NOT `years_experience_required` below, which is the LLM judge's
    # own reading of the same question. Two sources, two columns: merging them would
    # make the results CSV unable to say which produced any given value, and the
    # regex covers rows the judge never saw.
    yoe_required: Optional[float] = None

    # --- Clustering ----------------------------------------------------------
    # Set on jobs that inherited their score from a cluster representative rather
    # than being scored directly: the representative's job_id. See funnel/cluster.py.
    cluster_representative: Optional[str] = None
    # How many postings shared this job's cluster key, including itself.
    cluster_size: int = 1
    # The judge's own reading of the posting's YoE requirement. Nothing writes it any
    # more — the judge is no longer asked, because `yoe_required` above answers the same
    # question for free — and nothing renders it. It stays, like `rerank_score_wide`,
    # because rows written before the change carry it in `raw_json`.
    years_experience_required: Optional[float] = None
    # The judge's evidence checklist after `score_bands` has gated it: one dict per
    # requirement, with the quote it rests on. Stored only in `raw_json`; it is the
    # answer to "why this number" that the three rationales summarise.
    requirements: list[dict] = []
    core_skills_score: int = 0
    core_skills_rationale: str = ""
    experience_score: int = 0
    experience_rationale: str = ""
    education_bonus_score: int = 0
    education_rationale: str = ""
    match_reasons: list[str]
    disqualifiers: list[str]
    recommend: bool

    skipped: bool = False
    skip_reason: Optional[str] = None
    scored_at: datetime
    source_run_id: str


# ---------------------------------------------------------------------------
# LLMBackend protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class LLMBackend(Protocol):
    async def call(self, prompt: str, system_prompt: str) -> ScoringSchema: ...


# ---------------------------------------------------------------------------
# Gemini backend
# ---------------------------------------------------------------------------

def _is_gemini_retryable(exc: BaseException) -> bool:
    try:
        from google.genai import errors as genai_errors  # type: ignore[import-untyped]
        if isinstance(exc, genai_errors.ClientError):
            return getattr(exc, "code", None) == 429
        if isinstance(exc, genai_errors.ServerError):
            return True
    except ImportError:
        pass
    try:
        from google.api_core import exceptions as gexc
        return isinstance(exc, (gexc.ResourceExhausted, gexc.ServiceUnavailable, gexc.InternalServerError))
    except ImportError:
        pass
    return False


def _gemini_wait(retry_state) -> float:
    """Read retryDelay from the API error; fall back to 90s."""
    exc = retry_state.outcome.exception()
    if exc:
        m = re.search(r"'retryDelay':\s*'(\d+)s'", str(exc))
        if m:
            return float(m.group(1)) + 5
    return 90.0


_gemini_retry = retry(
    retry=retry_if_exception(_is_gemini_retryable),
    stop=stop_never,
    wait=_gemini_wait,
    reraise=True,
)


class GeminiBackend:
    def __init__(self, settings: MatcherSettings, sem: asyncio.Semaphore) -> None:
        from google import genai  # type: ignore[import-untyped]
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise EnvironmentError("GOOGLE_API_KEY environment variable is not set.")
        self._client = genai.Client(api_key=api_key)
        self._settings = settings
        self._sem = sem

    @_gemini_retry
    async def call(self, prompt: str, system_prompt: str) -> ScoringSchema:
        from google.genai import types  # type: ignore[import-untyped]
        async with self._sem:
            response = await self._client.aio.models.generate_content(
                model=self._settings.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ScoringSchema.model_json_schema(),
                    system_instruction=system_prompt,
                ),
            )
            if self._settings.request_interval_s > 0:
                await asyncio.sleep(self._settings.request_interval_s)
        return ScoringSchema.model_validate_json(response.text)


# ---------------------------------------------------------------------------
# OpenAI backend
# ---------------------------------------------------------------------------

def _is_openai_retryable(exc: BaseException) -> bool:
    try:
        import openai
        return isinstance(exc, (openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError))
    except ImportError:
        return False


def _openai_wait(retry_state) -> float:
    exc = retry_state.outcome.exception()
    if exc and hasattr(exc, "response") and exc.response is not None:
        after = exc.response.headers.get("Retry-After")
        if after:
            return float(after) + 2
    return 60.0


_openai_retry = retry(
    retry=retry_if_exception(_is_openai_retryable),
    stop=stop_never,
    wait=_openai_wait,
    reraise=True,
)


class OpenAIBackend:
    def __init__(self, settings: MatcherSettings, sem: asyncio.Semaphore) -> None:
        try:
            import openai
        except ImportError:
            raise ImportError("openai package required. Install with: pip install openai")
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise EnvironmentError("OPENAI_API_KEY environment variable is not set.")
        self._client = openai.AsyncOpenAI(api_key=api_key)
        self._settings = settings
        self._sem = sem

    @_openai_retry
    async def call(self, prompt: str, system_prompt: str) -> ScoringSchema:
        async with self._sem:
            response = await self._client.beta.chat.completions.parse(
                model=self._settings.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                response_format=ScoringSchema,
            )
            if self._settings.request_interval_s > 0:
                await asyncio.sleep(self._settings.request_interval_s)
        return response.choices[0].message.parsed


# ---------------------------------------------------------------------------
# Anthropic backend
# ---------------------------------------------------------------------------

def _is_anthropic_retryable(exc: BaseException) -> bool:
    try:
        import anthropic
        return isinstance(exc, (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError))
    except ImportError:
        return False


def _anthropic_wait(retry_state) -> float:
    exc = retry_state.outcome.exception()
    if exc and hasattr(exc, "response") and exc.response is not None:
        after = exc.response.headers.get("Retry-After")
        if after:
            return float(after) + 2
    return 60.0


_anthropic_retry = retry(
    retry=retry_if_exception(_is_anthropic_retryable),
    stop=stop_never,
    wait=_anthropic_wait,
    reraise=True,
)


class AnthropicBackend:
    def __init__(self, settings: MatcherSettings, sem: asyncio.Semaphore) -> None:
        try:
            import anthropic
        except ImportError:
            raise ImportError("anthropic package required. Install with: pip install anthropic")
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not set.")
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._settings = settings
        self._sem = sem

    @_anthropic_retry
    async def call(self, prompt: str, system_prompt: str) -> ScoringSchema:
        async with self._sem:
            response = await self._client.messages.create(
                model=self._settings.model,
                max_tokens=1024,
                system=system_prompt,
                messages=[{"role": "user", "content": prompt}],
                tools=[{
                    "name": "score_job",
                    "description": "Return the structured scoring result for the job-candidate match.",
                    "input_schema": ScoringSchema.model_json_schema(),
                }],
                tool_choice={"type": "tool", "name": "score_job"},
            )
            if self._settings.request_interval_s > 0:
                await asyncio.sleep(self._settings.request_interval_s)
        tool_use = next(b for b in response.content if b.type == "tool_use")
        return ScoringSchema.model_validate(tool_use.input)


# ---------------------------------------------------------------------------
# Claude Code backend — scores on the user's Claude subscription, not an API key
# ---------------------------------------------------------------------------

class CLILaunchError(RuntimeError):
    """`claude` never started — the host refused the process, the backend was never asked.

    A subclass of RuntimeError so `JobScorer.score` records it as `api_error` like any
    other failure (retryable: a job is never retired on an error). The distinct type is
    what lets the breaker's summary blame the machine rather than the backend.
    """


# Waits between attempts after a launch failure. Only launch failures are retried: exit
# 1 carries a real answer from the API in its stdout, and asking again changes nothing.
_LAUNCH_RETRY_DELAYS_S = (5.0, 20.0, 60.0)


class ClaudeCodeBackend:
    """Score through the local `claude` CLI so the user's Pro/Max subscription
    pays for it instead of a metered API key.

    Mirrors the tuner's ClaudeCodeOptimizerBackend, with one real difference: that
    one returns raw stdout as a str, while `LLMBackend.call` must return a
    ScoringSchema. Rather than regex-scraping prose we ask the CLI for structured
    output (`--output-format json --json-schema`) and validate the result.

    NOTE for whoever hits it: the headless docs say `--bare` "will become the default
    for `-p` in a future release", and separately that in bare mode "Claude Code never
    reads OAuth credentials or the system keychain". If both hold as written, a future
    CLI release stops `claude -p` from using the subscription — which is the entire
    basis of free scoring here. How auth is handled in that transition is not
    documented, so this is a thing to watch, not a thing to pre-empt. Do not add
    `--bare` to the argv below to "modernise" it; it would break scoring today.
    """

    def __init__(self, settings: MatcherSettings, sem: asyncio.Semaphore) -> None:
        if not shutil.which("claude"):
            raise EnvironmentError("claude CLI not found on PATH. Install Claude Code.")
        self._settings = settings
        self._sem = sem
        self._timeout = settings.claude_cli_timeout_s
        self._schema = json.dumps(ScoringSchema.model_json_schema())
        self.usage = UsageTally()

    @staticmethod
    def _env() -> dict[str, str]:
        # Subscription, never a pay-as-you-go key — see `claude_cli.subscription_env`.
        return claude_cli.subscription_env()

    async def call(self, prompt: str, system_prompt: str) -> ScoringSchema:
        # The wait sits out here, not inside `_invoke`, so a retry does not hold one
        # of the scoring slots while it sleeps.
        for delay in (*_LAUNCH_RETRY_DELAYS_S, None):
            try:
                stdout = await self._invoke(prompt, system_prompt)
                break
            except CLILaunchError as exc:
                if delay is None:
                    raise
                logger.warning("%s — retrying in %gs", exc, delay)
                await asyncio.sleep(delay)

        # Raise on anything unparseable rather than returning a half-built result:
        # JobScorer.score catches it and records a per-job skip, which is a far
        # better outcome than a bogus score or a dead run.
        raw = stdout.decode(errors="replace")
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"claude CLI returned non-JSON: {raw[:300]}") from exc

        # Read the meters before unwrapping — the counts live on the envelope, and
        # `_payload` throws it away. Recorded before validation deliberately: a call
        # whose payload fails to parse was still billed.
        self.usage.record(envelope)
        return self._payload(envelope)

    async def _invoke(self, prompt: str, system_prompt: str) -> bytes:
        """One `claude -p` run. Returns its stdout; raises on a non-zero exit."""
        async with self._sem:
            # `--json-schema` takes the schema *itself*, not a path to it — the CLI
            # parses the argument value as JSON. Passing a filename made every call
            # fail with "not valid JSON: Unexpected identifier" (the drive letter of
            # C:\Users\...), which silently scored nothing. The only real bound is
            # argv length; see test_scoring_resilience.py, which pins the schema well
            # under it.
            #
            # `--no-session-persistence` (print mode only) stops each scored job
            # leaving a transcript in ~/.claude/projects/. A 150-job sweep otherwise
            # writes 150 of them, and — because Claude Code generates a title for
            # every unnamed session with a background model request — bills the user
            # ~150 extra calls against the same allowance, for titles nobody opens.
            # `-p` sessions are already kept out of the /resume picker, so this is
            # about the litter and the cost, not visibility. The env-var equivalent,
            # CLAUDE_CODE_SKIP_PROMPT_HISTORY, suppresses transcripts in every mode;
            # the process environment belongs to the user, so prefer the per-call flag.
            #
            # `--safe-mode` and `--tools ""` strip everything a judge does not use: the
            # built-in tool schemas, user-level MCP servers, CLAUDE.md, skills, plugins
            # and hooks. Measured on one call (2026-09-16, Sonnet 5, effort low): 54,441
            # input tokens without them, 1,765 with. The resume prefix caches, but every
            # cache write — the first call of a sweep, each concurrent cold start, any
            # TTL miss — paid for those ~52k again. Structured output still works with
            # no tools, which was checked before this shipped.
            #
            # Two things not to "tidy":
            #   - `--safe-mode` is not `--bare`. Auth works normally under it; `--bare`
            #     never reads OAuth, which would end subscription scoring (NOTE above).
            #   - These flags belong HERE, not in `claude_cli`. The apply worker shares
            #     that module and needs an MCP server and tools to drive a browser;
            #     either flag there would silently break auto-apply.
            #     `tests/test_apply_worker.py` fails the build if they reach its argv.
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p",
                "--system-prompt", system_prompt,
                "--model", self._settings.model,
                "--effort", self._settings.effort,
                "--safe-mode",
                "--tools", "",
                "--no-session-persistence",
                "--output-format", "json",
                "--json-schema", self._schema,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=prompt.encode()),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError(f"claude CLI timed out after {self._timeout}s")
            if claude_cli.is_launch_failure(proc.returncode):
                raise CLILaunchError(
                    f"claude CLI exited {claude_cli.describe_exit(proc.returncode)}"
                )
            if proc.returncode != 0:
                # Report BOTH streams, each truncated on its own, stdout first.
                #
                # This used to be `stderr or stdout`, on the theory that stderr is empty
                # when the CLI reports a failure (a bad --model, for one) on stdout. That
                # does not hold: stderr carries routine warnings on every call — an
                # untrusted workspace alone is 645 characters — so the fallback never
                # fired, and one real failure was logged as a trust warning while five
                # more read "(no output)".
                #
                # Truncating the two together would not fix it either: a joined string cut
                # at 500 is still all stderr, because the warning outruns that cap by
                # itself. Hence a budget per stream, and stdout first.
                out = stdout.decode(errors="replace").strip()
                err = stderr.decode(errors="replace").strip()
                detail = " | ".join(
                    part for part in (
                        f"stdout: {out[:300]}" if out else "",
                        f"stderr: {err[:300]}" if err else "",
                    ) if part
                ) or "(no output)"
                raise RuntimeError(
                    f"claude CLI exited {proc.returncode}: {detail}"
                )
            if self._settings.request_interval_s > 0:
                await asyncio.sleep(self._settings.request_interval_s)
        return stdout

    @staticmethod
    def _payload(envelope) -> ScoringSchema:
        # Shared with the apply worker, which reads its outcome the same way.
        return ScoringSchema.model_validate(claude_cli.unwrap_envelope(envelope))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_BACKENDS: dict[str, type] = {
    "gemini": GeminiBackend,
    "openai": OpenAIBackend,
    "anthropic": AnthropicBackend,
    "claude_code": ClaudeCodeBackend,
}


def make_backend(settings: MatcherSettings, sem: asyncio.Semaphore) -> LLMBackend:
    provider = (settings.provider or os.environ.get("LLM_PROVIDER", "claude_code")).lower()
    cls = _BACKENDS.get(provider)
    if cls is None:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{provider}'. Choose from: {', '.join(_BACKENDS)}"
        )
    return cls(settings, sem)


# ---------------------------------------------------------------------------
# JobScorer
# ---------------------------------------------------------------------------

class JobScorer:
    def __init__(self, settings: MatcherSettings, backend: LLMBackend) -> None:
        self._settings = settings
        self._backend = backend
        # Text of the most recent backend failure. `score` deliberately converts a
        # failed call into an `api_error` result rather than raising, so this is the
        # only channel by which the reason reaches the caller — matcher.py's circuit
        # breaker quotes it, which is the difference between "0 new matches" and
        # "the scorer is broken, here is why".
        self.last_error: str | None = None
        # Whether that failure was the host refusing to start the CLI. Carried as a
        # flag rather than read back out of the message text.
        self.last_error_was_launch = False
        # Only backends that can read their own meters expose one; the rest leave
        # this None and the caller says nothing rather than reporting zeros as fact.
        self.usage: UsageTally | None = getattr(backend, "usage", None)

    async def score(self, job: Job, resume_text: str, run_id: str, projects_text: str = "") -> MatchResult:
        base = MatchResult(
            job_id=job.job_id,
            board_token=job.board_token,
            title=job.title,
            location=job.location.name,
            absolute_url=str(job.absolute_url),
            relevance_score=0,
            years_experience_required=None,
            match_reasons=[],
            disqualifiers=[],
            recommend=False,
            scored_at=datetime.now(timezone.utc),
            source_run_id=run_id,
        )

        if not job.content_text or not job.content_text.strip():
            return base.model_copy(update={"skipped": True, "skip_reason": "no_content_text"})

        candidate_profile = resume_text
        if projects_text:
            candidate_profile += f"\n\n## Additional Projects\n{projects_text}"

        # The resume goes in the SYSTEM PROMPT, not the message. That looks odd —
        # it is the candidate's data, not an instruction — and it is what makes
        # scoring affordable.
        #
        # Prompt caching is a prefix match, and the request layers as system prompt
        # then conversation. The rubric, the resume and the tool schema are byte-
        # identical for every job in a run; only the posting changes. Putting the
        # resume here makes that whole prefix the cached part, so the API bills it at
        # roughly a tenth from the second judged job onward instead of reprocessing
        # the resume 150 times. With the resume in the message, as it was, the
        # unchanging half sat *behind* changing content and could never be cached.
        #
        # Two things this depends on, both worth knowing before rearranging it:
        #   - Each `claude -p` call is its own conversation, but the API cache is
        #     keyed by model and prefix, not by session, so separate invocations
        #     share it. On a subscription the main conversation gets a one-hour TTL,
        #     comfortably longer than a sweep.
        #   - There is a minimum cacheable prefix, and it varies by model — 1,024
        #     tokens on Sonnet 5, but 4,096 on Haiku 4.5. Below it, caching silently
        #     does not happen. That is one of the reasons the shipped default is
        #     Sonnet; see MatcherSettings.model.
        # `UsageTally.cache_read` is how you check it is actually working.
        #
        # Both inputs are fenced in tags. The resume and the posting are data, and a
        # posting in particular is third-party text sitting beside instructions; the
        # tags are what the prompt refers to when it says which is which.
        system_prompt = f"{SYSTEM_PROMPT}\n<resume>\n{candidate_profile}\n</resume>\n"
        prompt = (
            f"<posting>\n## Job: {job.title} at {job.board_token}\n"
            f"{job.content_text[:self._settings.max_content_chars]}\n</posting>\n"
        )

        try:
            result = await self._backend.call(prompt, system_prompt)
        except Exception as exc:
            logger.warning("LLM call failed for job %s/%s: %s", job.board_token, job.job_id, exc)
            self.last_error = str(exc)
            self.last_error_was_launch = isinstance(exc, CLILaunchError)
            return base.model_copy(update={"skipped": True, "skip_reason": "api_error"})

        return base.model_copy(update=score_bands(result))
