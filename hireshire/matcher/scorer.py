from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel
from tenacity import retry, retry_if_exception, stop_never

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


class ScoringSchema(BaseModel):
    years_experience_required: Optional[float] = None
    core_skills_score: int
    core_skills_rationale: str
    experience_score: int
    experience_rationale: str
    education_bonus_score: int
    education_rationale: str
    match_reasons: list[str]
    disqualifiers: list[str]
    recommend: bool


class MatchResult(BaseModel):
    job_id: str
    board_token: str
    title: str
    location: str
    absolute_url: str

    relevance_score: Optional[int] = None  # None = never scored (skip_llm)

    # --- Funnel scores -------------------------------------------------------
    # Four numbers on FOUR DIFFERENT SCALES. Each is ordinal within itself and none
    # is comparable to any other — a rerank logit is not a percentage and the two
    # rerank stages do not share a range. Anything that sorts or displays them must
    # keep them in separate columns.
    #
    # Bi-encoder cosine (0-1) of the TITLE against the configured target roles.
    # The number that explains a `title_low_relevance` drop; it used to be computed
    # against the threshold and thrown away.
    encoder_score: Optional[float] = None
    # Wide-pass cross-encoder logit over the full description. Present for every
    # reranked job.
    rerank_score_wide: Optional[float] = None
    # Refined cross-encoder logit — a different model, so NOT comparable to
    # rerank_score_wide. Present only for the top `refine.depth`; this is the number
    # that decided whether the job was worth an LLM call. None when reranking was
    # off or the job never reached the refine pass.
    rerank_score: Optional[float] = None
    # Which stage last judged this job: "wide", "refined", or None.
    rerank_stage: Optional[str] = None

    # --- Clustering ----------------------------------------------------------
    # Set on jobs that inherited their score from a cluster representative rather
    # than being scored directly: the representative's job_id. See funnel/cluster.py.
    cluster_representative: Optional[str] = None
    # How many postings shared this job's cluster key, including itself.
    cluster_size: int = 1
    years_experience_required: Optional[float] = None
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

class ClaudeCodeBackend:
    """Score through the local `claude` CLI so the user's Pro/Max subscription
    pays for it instead of a metered API key.

    Mirrors the tuner's ClaudeCodeOptimizerBackend, with one real difference: that
    one returns raw stdout as a str, while `LLMBackend.call` must return a
    ScoringSchema. Rather than regex-scraping prose we ask the CLI for structured
    output (`--output-format json --json-schema`) and validate the result.
    """

    def __init__(self, settings: MatcherSettings, sem: asyncio.Semaphore) -> None:
        if not shutil.which("claude"):
            raise EnvironmentError("claude CLI not found on PATH. Install Claude Code.")
        self._settings = settings
        self._sem = sem
        self._timeout = settings.claude_cli_timeout_s
        self._schema = json.dumps(ScoringSchema.model_json_schema())

    @staticmethod
    def _env() -> dict[str, str]:
        # The CLI prefers ANTHROPIC_API_KEY over the subscription login, which
        # silently bills pay-as-you-go credits and then fails with "Credit
        # balance is too low". load_dotenv() may well have put one in our
        # environment for the BYO-key path, so strip both auth vars here.
        return {
            k: v for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        }

    async def call(self, prompt: str, system_prompt: str) -> ScoringSchema:
        async with self._sem:
            # `--json-schema` takes the schema *itself*, not a path to it — the CLI
            # parses the argument value as JSON. Passing a filename made every call
            # fail with "not valid JSON: Unexpected identifier" (the drive letter of
            # C:\Users\...), which silently scored nothing. The only real bound is
            # argv length; see test_scoring_resilience.py, which pins the schema well
            # under it.
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p",
                "--system-prompt", system_prompt,
                "--model", self._settings.model,
                "--effort", self._settings.effort,
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
            if proc.returncode != 0:
                # The CLI reports some failures (a bad --model, for one) on stdout and
                # leaves stderr empty. Falling back keeps the circuit breaker's summary
                # from reading "claude CLI exited 1:" with nothing after the colon.
                detail = stderr.decode(errors="replace").strip() or \
                    stdout.decode(errors="replace").strip() or "(no output)"
                raise RuntimeError(
                    f"claude CLI exited {proc.returncode}: {detail[:500]}"
                )
            if self._settings.request_interval_s > 0:
                await asyncio.sleep(self._settings.request_interval_s)

        # Raise on anything unparseable rather than returning a half-built result:
        # JobScorer.score catches it and records a per-job skip, which is a far
        # better outcome than a bogus score or a dead run.
        return self._parse(stdout.decode(errors="replace"))

    @staticmethod
    def _parse(raw: str) -> ScoringSchema:
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"claude CLI returned non-JSON: {raw[:300]}") from exc

        # `--output-format json` wraps the answer; the payload has moved between
        # CLI versions, so accept the envelope itself or any of the usual keys.
        if isinstance(envelope, dict):
            for key in ("result", "response", "content", "output"):
                if key in envelope:
                    envelope = envelope[key]
                    break
        if isinstance(envelope, str):
            try:
                envelope = json.loads(envelope)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"claude CLI payload was not JSON: {envelope[:300]}"
                ) from exc
        return ScoringSchema.model_validate(envelope)


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

        prompt = (
            f"## Candidate Resume\n{candidate_profile}\n\n"
            f"## Job: {job.title} at {job.board_token}\n"
            f"{job.content_text[:self._settings.max_content_chars]}\n\n"
            #"Score how well this candidate matches this job. Be specific and evidence-based."
        )

        try:
            result = await self._backend.call(prompt, SYSTEM_PROMPT)
        except Exception as exc:
            logger.warning("LLM call failed for job %s/%s: %s", job.board_token, job.job_id, exc)
            self.last_error = str(exc)
            return base.model_copy(update={"skipped": True, "skip_reason": "api_error"})

        relevance_score = min(100, result.core_skills_score + result.experience_score + result.education_bonus_score)

        return base.model_copy(update={
            "relevance_score": relevance_score,
            "years_experience_required": result.years_experience_required,
            "core_skills_score": result.core_skills_score,
            "core_skills_rationale": result.core_skills_rationale,
            "experience_score": result.experience_score,
            "experience_rationale": result.experience_rationale,
            "education_bonus_score": result.education_bonus_score,
            "education_rationale": result.education_rationale,
            "match_reasons": result.match_reasons,
            "disqualifiers": result.disqualifiers,
            "recommend": result.recommend,
        })
