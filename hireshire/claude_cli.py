"""Rules every `claude -p` call the engine makes has to follow, kept in one place.

The scorer and the apply worker both shell out to the local CLI on the user's
subscription. Two copies of these rules is how the one that matters most — stripping
the API key — gets dropped from the second copy.

The Codex backend shares what is not about `claude` itself: the exit-code readers and
`exit_detail`. It is the same host starting the same kind of console child, and a
failure has to read the same way in the log whichever CLI produced it.
"""

from __future__ import annotations

import json
import os
from typing import Any

_API_AUTH_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# Exit codes meaning Windows never got `claude.exe` running, so the CLI said nothing and
# nothing was billed. Observed: every scoring call of a monitor cycle exited 0xC0000142
# at once — the first with no other call in flight and no apply session running — two
# hours after the same process had scored 31 jobs. That is the host refusing to start a
# console child, not the backend failing, and the log needs to say so. The codex
# backend shares these: it is the same host refusing the same kind of child.
LAUNCH_FAILURE_CODES = {
    0xC0000142: "STATUS_DLL_INIT_FAILED: Windows could not start the CLI process",
}


def is_launch_failure(returncode: int | None) -> bool:
    """True when the exit code means the CLI process never started."""
    return returncode is not None and (returncode & 0xFFFFFFFF) in LAUNCH_FAILURE_CODES


def describe_exit(returncode: int | None) -> str:
    """The exit code, with its NTSTATUS name when it is a known launch failure.

    Matched on the unsigned value, so the signed spelling (-1073741502) is caught too.
    """
    if not is_launch_failure(returncode):
        return str(returncode)
    code = returncode & 0xFFFFFFFF
    return f"{returncode} (0x{code:08X} {LAUNCH_FAILURE_CODES[code]})"


def exit_detail(out: str, stderr: bytes) -> str:
    """Both streams of a failed CLI call, each truncated on its own, stdout first.

    This used to be `stderr or stdout`, on the theory that stderr is empty when the
    CLI reports a failure (a bad --model, for one) on stdout. That does not hold:
    stderr carries routine warnings on every call — an untrusted workspace alone is
    645 characters — so the fallback never fired, and one real failure was logged as
    a trust warning while five more read "(no output)".

    Truncating the two together would not fix it either: a joined string cut at 500
    is still all stderr, because the warning outruns that cap by itself. Hence a
    budget per stream, and stdout first. `out` is text because every caller passes
    the failure it parsed — `envelope_failure` here, `codex_cli.parse_events` there —
    in place of the raw stream it came from.
    """
    out = out.strip()
    err = stderr.decode(errors="replace").strip()
    return " | ".join(
        part for part in (
            f"stdout: {out[:300]}" if out else "",
            f"stderr: {err[:300]}" if err else "",
        ) if part
    ) or "(no output)"


def envelope_failure(raw: str) -> str | None:
    """Why a `--output-format json` call failed, in the CLI's own words.

    Nine apply sessions failed `exited 1` across one night and not one logged a
    reason: the envelope opens with `duration_api_ms`, `session_id`, `total_cost_usd`
    and a `usage` block, so clipping its raw text for the log spent the whole budget
    on token counts and stopped short of `result`. Reading the fields by name is what
    makes the next failure name itself.

    `api_ms=0` is reported only when it is zero, because that is the one value worth
    a word: a session reporting no API time never reached the model, which is what
    separates "ran, then failed" from "was refused at the door" — the distinction the
    night's bursts had to be diagnosed by hand.

    Returns None when there is nothing to say, so the caller falls back to the raw
    streams. Never raises: every caller is already reporting a failure.
    """
    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(envelope, dict):
        return None

    parts: list[str] = []
    if "is_error" in envelope:
        parts.append(f"is_error={str(envelope['is_error']).lower()}")
    subtype = envelope.get("subtype")
    if isinstance(subtype, str) and subtype.strip():
        parts.append(f"subtype={subtype.strip()}")
    if envelope.get("duration_api_ms") == 0:
        parts.append("api_ms=0")

    # `result` is the CLI's prose on a failed call; older versions said `error`, and
    # some spell it as an object with a message inside.
    reason = envelope.get("result")
    if not isinstance(reason, str) or not reason.strip():
        error = envelope.get("error")
        reason = error.get("message") if isinstance(error, dict) else error
    if isinstance(reason, str) and reason.strip():
        # One log line, whatever the CLI wrapped.
        parts.append(f"result: {' '.join(reason.split())[:300]}")

    return " | ".join(parts) or None


def subscription_env() -> dict[str, str]:
    """The process environment without the API auth variables.

    The CLI prefers ANTHROPIC_API_KEY over the claude.ai subscription login, which
    silently bills pay-as-you-go credits and then fails with "Credit balance is too
    low". `load_dotenv()` may well have put one in our environment for the BYO-key
    scoring path, so both are stripped for every CLI call.
    """
    return {k: v for k, v in os.environ.items() if k not in _API_AUTH_VARS}


def unwrap_envelope(envelope: Any) -> Any:
    """The answer inside a `--output-format json` envelope, JSON-decoded if it is text.

    The payload has moved between CLI versions, so accept the envelope itself or any
    of the usual keys. `structured_output` is where the current CLI puts a
    `--json-schema` result — the rest are kept because older versions used them and
    the engine has no way to know which version is on PATH.

    Raises RuntimeError when the payload is text that is not JSON.
    """
    if isinstance(envelope, dict):
        for key in ("structured_output", "result", "response", "content", "output"):
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
    return envelope
