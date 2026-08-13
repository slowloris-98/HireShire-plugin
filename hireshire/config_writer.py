"""Whitelisted, comment-preserving writer for the user's config YAMLs.

/hireshire:setup is conversational and never shows the user a YAML file, but it
still has to edit real YAML — and those files carry the comments that explain
every knob. A naive load-and-dump would strip them, so this uses ruamel round-trip
mode and preserves the original line endings too, keeping a one-key edit to a
one-line diff.

Two safety properties, both inherited from the dashboard config editor this was
extracted from (`webapp/routers/config_api.py`, minus the FastAPI layer):

* only whitelisted fields can be written — a typo'd key is rejected rather than
  silently added to the file
* the patched document is validated against the phase's own pydantic settings
  model *before* anything is written, so a bad value can never land on disk

Paths resolve under the user's config dir in the plugin data directory. The
shipped defaults in the install dir are read-only and replaced on every update.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import ValidationError
from ruamel.yaml import YAML

from hireshire import paths
from hireshire.applier.config import ApplierSettings
from hireshire.config import ScraperSettings
from hireshire.funnel.config import FunnelConfig
from hireshire.matcher.config import MatcherSettings, TitleFilterConfig

# Scoring providers. `claude_code` runs through the local Claude CLI on the user's
# subscription and is the default; the rest need an API key in the environment.
PROVIDERS = ["claude_code", "openai", "anthropic", "gemini"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


@dataclass
class FieldSpec:
    path: tuple[str, ...]          # key path into the YAML document
    type: str                      # bool | int | float | str | str_list | enum
    doc: str                       # human-readable description
    options: Optional[list[str]] = None
    # Applied to the incoming value before it is written, after the `type`-driven
    # coercion in _COERCIONS. The pydantic models validate a *copy* of the document,
    # so any coercion a field_validator performs is discarded — it can reject a value
    # but cannot clean one. Fields that need the stored form tidied (rather than
    # merely checked) say so here; anything a whole type needs goes in _COERCIONS.
    normalise: Optional[Callable[[Any], Any]] = None


def _clean_path_value(v: Any) -> Any:
    """Strip the quotes Windows Explorer's "Copy as path" adds, and expand `~`."""
    if not isinstance(v, str):
        return v
    v = v.strip().strip('"').strip("'")
    return str(Path(v).expanduser()) if v else ""


def _as_str_list(v: Any) -> Any:
    """Wrap a bare string in a list.

    Setup asks for locations and job titles in plain English, so "united states" is
    the natural thing to pass, and pydantic can reject it but not clean it. Anything
    that is not a string is left alone, so a genuinely wrong type still fails
    validation rather than being silently coerced into something plausible.

    Deliberately does NOT split on commas: "San Francisco, CA" is one location, and
    guessing otherwise would quietly turn it into two that match nothing.
    """
    if isinstance(v, str):
        return [v] if v.strip() else []
    return v


# Coercions keyed by FieldSpec.type, applied before the per-field `normalise`. This
# is what makes `type` load-bearing rather than documentation: a list field added
# later gets the same treatment without anyone having to remember.
_COERCIONS: dict[str, Callable[[Any], Any]] = {"str_list": _as_str_list}


@dataclass
class PhaseSpec:
    file: str
    fields: dict[str, FieldSpec]
    validate: Callable[[dict], Any]


def _validate_scraper(d: dict) -> Any:
    return ScraperSettings(**d.get("settings", {}))


def _validate_matcher_file(d: dict) -> Any:
    MatcherSettings(**d.get("settings", {}))
    TitleFilterConfig(**d.get("title_filter", {}))
    FunnelConfig(**d.get("funnel", {}))
    return True


def _validate_applier(d: dict) -> Any:
    return ApplierSettings(**d.get("settings", {}))


PHASE_SPECS: dict[str, PhaseSpec] = {
    "scraper": PhaseSpec(
        file="scraper.yaml",
        validate=_validate_scraper,
        fields={
            "location_filter": FieldSpec(
                ("settings", "location_filter"), "str_list",
                "Locations to keep. Case-insensitive substring match; empty means no filter.",
            ),
            "max_age_hours": FieldSpec(
                ("settings", "max_age_hours"), "int",
                "Only keep jobs posted in the last N hours.",
            ),
            "enabled_platforms": FieldSpec(
                ("settings", "enabled_platforms"), "str_list",
                "Which job boards to sweep. Adding workday and bamboohr roughly "
                "triples the company count and the run time.",
            ),
            "poll_interval_hours": FieldSpec(
                ("settings", "poll_interval_hours"), "float",
                "How often /hireshire:start-orchestration re-sweeps, in hours.",
            ),
            "workspace_dir": FieldSpec(
                ("settings", "workspace_dir"), "str",
                "Absolute path to the user's job-search folder. Their resume copy "
                "and every run's results live under it. Captured once at setup.",
                normalise=_clean_path_value,
            ),
        },
    ),
    "matcher": PhaseSpec(
        file="matcher.yaml",
        validate=_validate_matcher_file,
        fields={
            "threshold": FieldSpec(
                ("settings", "threshold"), "int",
                "Minimum 0-100 relevance score for a job to be shortlisted.",
            ),
            "provider": FieldSpec(
                ("settings", "provider"), "enum",
                "Which LLM scores jobs.", options=PROVIDERS,
            ),
            "model": FieldSpec(("settings", "model"), "str", "Model name for the chosen provider."),
            "effort": FieldSpec(
                ("settings", "effort"), "enum",
                "Thinking level for the claude_code provider.", options=EFFORTS,
            ),
            "resume_path": FieldSpec(
                ("settings", "resume_path"), "str", "Path to the resume PDF.",
                normalise=_clean_path_value,
            ),
            "search_profile_path": FieldSpec(
                ("settings", "search_profile_path"), "str",
                "Generated candidate profile used as the reranker query.",
            ),
            "skip_llm": FieldSpec(
                ("settings", "skip_llm"), "bool",
                "Skip LLM scoring; shortlist everything that passes the title gate.",
            ),
            "include_keywords": FieldSpec(
                ("title_filter", "include_keywords"), "str_list",
                "Title must contain at least one of these.",
            ),
            "exclude_keywords": FieldSpec(
                ("title_filter", "exclude_keywords"), "str_list",
                "Title must contain none of these.",
            ),
        },
    ),
    "funnel": PhaseSpec(
        file="matcher.yaml",
        validate=_validate_matcher_file,
        fields={
            "enabled": FieldSpec(("funnel", "enabled"), "bool", "Run the relevance funnel."),
            "targets": FieldSpec(
                ("funnel", "encoder", "targets"), "str_list",
                "Adjacent job titles the candidate is qualified for.",
            ),
            # Named `encoder_threshold`, not `threshold`, on purpose. The matcher
            # phase has a `threshold` too — a 0-100 LLM cut-off, in the same file,
            # asked two steps apart during setup. Both are floats to pydantic, so
            # writing the LLM's 85 here would validate cleanly and silently destroy
            # the recall net. Distinct names make that mistake impossible to make.
            "encoder_threshold": FieldSpec(
                ("funnel", "encoder", "threshold"), "float",
                "Minimum title cosine similarity, 0-1. Keep low — this is a recall net, "
                "not the matcher's 0-100 relevance threshold.",
            ),
            "rerank_enabled": FieldSpec(
                ("funnel", "rerank", "enabled"), "bool",
                "Rank candidates with a cross-encoder before spending the LLM budget.",
            ),
            "top_k": FieldSpec(
                ("funnel", "top_k"), "int",
                "How many jobs get an LLM score per run. 0 means no cap.",
            ),
        },
    ),
    "applier": PhaseSpec(
        file="applier.yaml",
        validate=_validate_applier,
        fields={
            "enable_applier": FieldSpec(
                ("settings", "enable_applier"), "bool",
                "Run the applier after each pipeline run.",
            ),
            "dry_run": FieldSpec(
                ("settings", "dry_run"), "bool",
                "Fill every form but never click submit.",
            ),
            "resume_path": FieldSpec(
                ("settings", "resume_path"), "str", "Resume PDF to upload.",
                normalise=_clean_path_value,
            ),
            "first_name": FieldSpec(("settings", "first_name"), "str", "Applicant first name."),
            "last_name": FieldSpec(("settings", "last_name"), "str", "Applicant last name."),
            "email": FieldSpec(("settings", "email"), "str", "Applicant email."),
            "phone": FieldSpec(("settings", "phone"), "str", "Applicant phone."),
            "generate_cover_letter": FieldSpec(
                ("settings", "generate_cover_letter"), "bool",
                "Write a cover letter when the form asks for one.",
            ),
        },
    ),
}


class ConfigError(ValueError):
    """A patch was rejected: unknown phase, non-whitelisted field, or a value the
    phase's settings model refuses."""


_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096  # don't wrap long comment lines
# Match the config/*.yaml style: list items indented 4 with the dash at 2, so a
# one-key edit doesn't reflow every sequence line.
_yaml.indent(mapping=2, sequence=4, offset=2)


def _load_doc(path: Path):
    return _yaml.load(path.read_text(encoding="utf-8"))


def _dump_doc(doc, path: Path) -> None:
    buf = io.StringIO()
    _yaml.dump(doc, buf)
    text = buf.getvalue()
    # ruamel always emits LF; preserve the file's original newline style (the
    # config/*.yaml files are CRLF on Windows) so a one-key edit produces a
    # one-line diff instead of rewriting every line.
    if path.exists() and b"\r\n" in path.read_bytes():
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    path.write_bytes(text.encode("utf-8"))


def _get_path(doc, path: tuple[str, ...]) -> Any:
    node = doc
    for key in path:
        if node is None or key not in node:
            return None
        node = node[key]
    return node


def _set_path(doc, path: tuple[str, ...], value: Any) -> None:
    node = doc
    for key in path[:-1]:
        if key not in node or node[key] is None:
            node[key] = {}
        node = node[key]
    node[path[-1]] = value


def _plain(value: Any) -> Any:
    """Coerce ruamel scalar/collection wrappers into plain Python types, which is
    what pydantic wants to validate against."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if value is None:
        return None
    return str(value)


def install_user_config() -> list[Path]:
    """Copy the shipped default YAMLs into the user's data dir, once.

    Everything downstream reads from there, so plugin updates — which replace the
    install dir wholesale — can never clobber the user's answers. Existing files
    are left alone.
    """
    paths.ensure_data_dirs()
    installed = []
    for name in ("scraper.yaml", "matcher.yaml", "applier.yaml"):
        src = paths.SHIPPED_CONFIG / name
        dst = paths.USER_CONFIG / name
        if src.exists() and not dst.exists():
            dst.write_bytes(src.read_bytes())
            installed.append(dst)
    return installed


def read_config(phase: str) -> dict[str, Any]:
    if phase not in PHASE_SPECS:
        raise ConfigError(f"Unknown config phase {phase!r}. Choose from: {', '.join(PHASE_SPECS)}")
    spec = PHASE_SPECS[phase]
    path = paths.USER_CONFIG / spec.file
    if not path.exists():
        path = paths.SHIPPED_CONFIG / spec.file
    doc = _load_doc(path)
    return {name: _plain(_get_path(doc, fs.path)) for name, fs in spec.fields.items()}


def write_config(phase: str, values: dict[str, Any]) -> dict[str, Any]:
    """Apply `values` to the phase's YAML and return the file's new whitelisted state.

    The patch is applied to an in-memory copy and the whole document is validated
    before anything touches disk, so a rejected value leaves the file untouched.
    """
    if phase not in PHASE_SPECS:
        raise ConfigError(f"Unknown config phase {phase!r}. Choose from: {', '.join(PHASE_SPECS)}")
    spec = PHASE_SPECS[phase]

    unknown = [k for k in values if k not in spec.fields]
    if unknown:
        # A rejected key whose value is a dict of *valid* keys means the caller passed
        # the YAML nesting — `{"title_filter": {"exclude_keywords": [...]}}` instead of
        # `{"exclude_keywords": [...]}`. That is the shape the setup skill's own field
        # names suggest, so name the right call rather than only the wrong one.
        nested = sorted({
            child
            for k in unknown if isinstance(values[k], dict)
            for child in values[k] if child in spec.fields
        })
        hint = ""
        if nested:
            args = ", ".join(f"{c!r}: ..." for c in nested)
            hint = (
                f" These take flat keys, not the YAML nesting: "
                f"write_config({phase!r}, {{{args}}})."
            )
        raise ConfigError(
            f"Not editable for phase {phase!r}: {unknown}. "
            f"Allowed: {sorted(spec.fields)}.{hint}"
        )

    install_user_config()
    path = paths.USER_CONFIG / spec.file
    if not path.exists():
        raise ConfigError(f"No config file at {path}. Run /hireshire:setup first.")

    doc = _load_doc(path)
    for key, value in values.items():
        fs = spec.fields[key]
        coerce = _COERCIONS.get(fs.type)
        if coerce is not None:
            value = coerce(value)
        _set_path(doc, fs.path, fs.normalise(value) if fs.normalise else value)

    try:
        spec.validate(_plain(doc))
    except ValidationError as exc:
        raise ConfigError(f"Invalid config for {phase}: {exc}") from exc

    _dump_doc(doc, path)
    return {name: _plain(_get_path(doc, fs.path)) for name, fs in spec.fields.items()}


def field_docs(phase: str) -> dict[str, str]:
    if phase not in PHASE_SPECS:
        raise ConfigError(f"Unknown config phase {phase!r}")
    return {name: fs.doc for name, fs in PHASE_SPECS[phase].fields.items()}
