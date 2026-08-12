from __future__ import annotations

import email.utils
import random
from datetime import datetime, timezone

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    return isinstance(exc, (httpx.ConnectError, httpx.TimeoutException))


def _parse_retry_after(value: str) -> float | None:
    """Retry-After is either delta-seconds or an HTTP-date (RFC 7231)."""
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    parsed = email.utils.parsedate_to_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def make_retry_decorator(attempts: int):
    # Server's Retry-After is the most reliable backoff signal; honor it when the
    # response carries one, otherwise fall back to exponential backoff + jitter.
    _exp = wait_exponential(multiplier=2, min=1, max=60)

    def _wait(retry_state) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, httpx.HTTPStatusError):
            header = exc.response.headers.get("retry-after")
            if header:
                seconds = _parse_retry_after(header)
                if seconds is not None:
                    return min(seconds, 120.0) + random.uniform(0, 0.5)
        return _exp(retry_state) + random.uniform(0, 0.5)

    return retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(attempts),
        wait=_wait,
        reraise=True,
    )


def build_client(timeout_s: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_s),
        headers={"User-Agent": "HireShire/0.1 (job scraper)"},
        follow_redirects=True,
    )
