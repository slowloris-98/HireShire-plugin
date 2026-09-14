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
