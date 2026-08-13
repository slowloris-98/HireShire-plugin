"""Config writer tests.

/hireshire:setup never shows the user a YAML file, so these files are only ever
edited programmatically — which makes two properties load-bearing: the comments
that explain each knob must survive, and a bad value must never reach disk.
"""
from __future__ import annotations

import pytest

from hireshire import config_writer as cw
from hireshire import paths


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the plugin data dir at tmp so nothing touches the real install."""
    monkeypatch.setattr(paths, "DATA", tmp_path)
    monkeypatch.setattr(paths, "USER_CONFIG", tmp_path / "config")
    monkeypatch.setattr(paths, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(paths, "LOGS_DIR", tmp_path / "logs")
    cw.install_user_config()
    return tmp_path


def test_install_copies_shipped_defaults_once(data_dir):
    matcher = data_dir / "config" / "matcher.yaml"
    assert matcher.exists()

    matcher.write_text("settings: {threshold: 1}\n", encoding="utf-8")
    cw.install_user_config()
    # A second install must not clobber the user's answers — the whole reason
    # config lives in the data dir rather than the install dir.
    assert "threshold: 1" in matcher.read_text(encoding="utf-8")


def test_write_preserves_comments_and_inline_annotations(data_dir):
    cw.write_config("matcher", {"threshold": 82})
    text = (data_dir / "config" / "matcher.yaml").read_text(encoding="utf-8")

    assert "threshold: 82" in text
    # The comment on the edited line, and the block comments elsewhere, survive.
    assert "# min relevance_score (0-100) to shortlist a job" in text
    assert "recall net" in text


def test_write_rejects_non_whitelisted_fields(data_dir):
    with pytest.raises(cw.ConfigError, match="Not editable"):
        cw.write_config("matcher", {"db_path": "/etc/passwd"})


def test_a_bare_string_is_accepted_for_a_list_field(data_dir):
    """Setup asks for locations in plain English, so "united states" is the natural
    answer — and pydantic can reject that but never clean it."""
    cw.write_config("scraper", {"location_filter": "united states"})
    assert cw.read_config("scraper")["location_filter"] == ["united states"]


def test_an_empty_string_clears_a_list_field(data_dir):
    cw.write_config("scraper", {"location_filter": "  "})
    assert cw.read_config("scraper")["location_filter"] == []


def test_a_location_containing_a_comma_stays_one_entry(data_dir):
    """Splitting on commas would quietly turn one real location into two that match
    nothing."""
    cw.write_config("scraper", {"location_filter": "San Francisco, CA"})
    assert cw.read_config("scraper")["location_filter"] == ["San Francisco, CA"]


def test_the_yaml_nesting_is_rejected_and_names_the_flat_key(data_dir):
    """`{"title_filter": {"exclude_keywords": [...]}}` is the shape the field's own
    name suggests, so the error has to point at the call that works."""
    with pytest.raises(cw.ConfigError, match="flat keys") as exc:
        cw.write_config("matcher", {"title_filter": {"exclude_keywords": ["intern"]}})
    assert "'exclude_keywords'" in str(exc.value)


def test_write_rejects_unknown_phase(data_dir):
    with pytest.raises(cw.ConfigError, match="Unknown config phase"):
        cw.write_config("tuner", {"anything": 1})


@pytest.mark.parametrize("phase,patch", [
    ("matcher", {"effort": "turbo"}),          # not one of low|medium|high|xhigh|max
    ("matcher", {"threshold": "very high"}),   # not an int
    ("matcher", {"threshold": 150}),           # outside 0-100
    ("funnel", {"encoder_threshold": 85}),     # the matcher's scale, not the cosine one
])
def test_invalid_values_never_reach_disk(data_dir, phase, patch):
    path = data_dir / "config" / "matcher.yaml"
    before = path.read_text(encoding="utf-8")

    with pytest.raises(cw.ConfigError):
        cw.write_config(phase, patch)

    assert path.read_text(encoding="utf-8") == before, "a rejected patch must not write"


def test_the_funnel_has_no_key_called_threshold(data_dir):
    """Both phases write matcher.yaml and both once had a `threshold`: 0-100 for the
    LLM, 0-1 for the cosine recall net. Writing 85 into the funnel validated cleanly
    and silently rejected every job in the sweep. Distinct names prevent it."""
    with pytest.raises(cw.ConfigError, match="Not editable"):
        cw.write_config("funnel", {"threshold": 85})

    cw.write_config("funnel", {"encoder_threshold": 0.3})
    assert cw.read_config("funnel")["encoder_threshold"] == 0.3


def test_skill_documents_only_real_config_keys():
    """The setup skill lists the flat keys in a table. A rename that misses it would
    leave the skill teaching a call that fails at runtime, in front of the user."""
    import re
    skill = (paths.ROOT / "skills" / "setup" / "SKILL.md").read_text(encoding="utf-8")
    table = re.search(r"\| phase \| keys \|.*?\n\n", skill, re.DOTALL)
    assert table, "the phase/keys table is gone — did the skill get rewritten?"

    for line in table.group(0).splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 2 or not cells[0].startswith("`"):
            continue
        phase = cells[0].strip("`")
        assert phase in cw.PHASE_SPECS, f"SKILL.md names unknown phase {phase!r}"
        for key in re.findall(r"`([^`]+)`", cells[1]):
            assert key in cw.PHASE_SPECS[phase].fields, (
                f"SKILL.md documents {key!r} for phase {phase!r}, which is not writable"
            )


def test_funnel_and_matcher_share_one_file(data_dir):
    """Both phases edit matcher.yaml; writing one must not drop the other's keys."""
    cw.write_config("matcher", {"threshold": 90})
    cw.write_config("funnel", {"top_k": 42})

    assert cw.read_config("matcher")["threshold"] == 90
    assert cw.read_config("funnel")["top_k"] == 42


def test_setup_can_write_the_generated_search_profile_and_keywords(data_dir):
    """The three artifacts setup derives from the resume all land in one file."""
    cw.write_config("matcher", {
        "include_keywords": ["designer", "brand"],
        "exclude_keywords": ["intern"],
        "search_profile_path": "profile.md",
    })
    cw.write_config("funnel", {"targets": ["product designer", "brand strategist"]})

    matcher = cw.read_config("matcher")
    assert matcher["include_keywords"] == ["designer", "brand"]
    assert matcher["search_profile_path"] == "profile.md"
    assert cw.read_config("funnel")["targets"] == ["product designer", "brand strategist"]


def test_shipped_defaults_carry_no_personal_or_role_specific_data():
    """A user hunting non-technical roles must not inherit someone else's field,
    and no real contact details may ship in the package."""
    matcher = (paths.SHIPPED_CONFIG / "matcher.yaml").read_text(encoding="utf-8")
    applier = (paths.SHIPPED_CONFIG / "applier.yaml").read_text(encoding="utf-8")
    scraper_yaml = (paths.SHIPPED_CONFIG / "scraper.yaml").read_text(encoding="utf-8")

    for leaked in ("Udayan", "udayan", "6693406033", "@gmail.com"):
        assert leaked not in matcher and leaked not in applier
        assert leaked not in scraper_yaml

    import yaml
    m = yaml.safe_load(matcher)
    assert m["title_filter"]["include_keywords"] == []
    assert m["title_filter"]["exclude_keywords"] == []
    assert m["funnel"]["encoder"]["targets"] == []

    a = yaml.safe_load(applier)
    # Both safety gates ship in the safe position.
    assert a["settings"]["enable_applier"] is False
    assert a["settings"]["dry_run"] is True
    assert a["settings"]["email"] == ""

    # workspace_dir is the first setting whose natural value is an absolute path on
    # the packager's own machine, so it is the likeliest thing to ship by accident.
    s = yaml.safe_load(scraper_yaml)
    assert s["settings"]["workspace_dir"] == ""
