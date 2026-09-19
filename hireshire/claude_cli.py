"""Rules every `claude -p` call the engine makes has to follow, kept in one place.

The scorer and the apply worker both shell out to the local CLI on the user's
subscription. Two copies of these rules is how the one that matters most — stripping
the API key — gets dropped from the second copy.
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
# console child, not the backend failing, and the log needs to say so.
LAUNCH_FAILURE_CODES = {
    0xC0000142: "STATUS_DLL_INIT_FAILED: Windows could not start claude.exe",
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
