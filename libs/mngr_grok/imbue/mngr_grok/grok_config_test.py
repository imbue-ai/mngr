import json
import shlex
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

import pytest
import tomlkit

from imbue.mngr.errors import ConfigParseError
from imbue.mngr.primitives import CommandString
from imbue.mngr_grok.grok_config import COMPAT_CELL_NAMES
from imbue.mngr_grok.grok_config import GROK_HOOK_EVENT_NAMES
from imbue.mngr_grok.grok_config import GrokRootTurn
from imbue.mngr_grok.grok_config import MalformedGrokConfigError
from imbue.mngr_grok.grok_config import RenderedGrokConfig
from imbue.mngr_grok.grok_config import SessionBucketNameError
from imbue.mngr_grok.grok_config import build_config_toml
from imbue.mngr_grok.grok_config import build_hooks_json
from imbue.mngr_grok.grok_config import build_launch_command
from imbue.mngr_grok.grok_config import build_launch_env
from imbue.mngr_grok.grok_config import build_pinned_grok_environment
from imbue.mngr_grok.grok_config import build_removed_grok_environment_names
from imbue.mngr_grok.grok_config import build_requirements_toml
from imbue.mngr_grok.grok_config import compute_session_bucket_name
from imbue.mngr_grok.grok_config import get_grok_home
from imbue.mngr_grok.grok_config import merge_consent_answers
from imbue.mngr_grok.grok_config import parse_grok_config_text
from imbue.mngr_grok.grok_config import parse_rendered_model_settings
from imbue.mngr_grok.grok_config import parse_root_turn
from imbue.mngr_grok.grok_config import render_rendered_model_settings
from imbue.mngr_grok.grok_config import render_toml
from imbue.mngr_grok.grok_config import strip_claude_plugin_marketplace_suffixes
from imbue.mngr_grok.resources import grok_hook
from imbue.mngr_grok.resources.testing import CONFIG_FIXTURES_DIR

_ACKED_AT: Final[datetime] = datetime(2026, 10, 19, 18, 0, 0, tzinfo=timezone.utc)

_DEFAULT_CONFIG_INPUTS: Final[dict[str, Any]] = {
    "model": None,
    "reasoning_effort": None,
    "is_auto_allow_permissions": False,
    "is_self_update_disabled": True,
    "is_privacy_pinned": True,
    "is_memory_enabled": False,
    "is_isolated_from_claude_config": True,
    "subagent_names_to_disable": (),
    "enabled_claude_plugin_keys": (),
    "privacy_banner_acked_at": _ACKED_AT,
    "existing_agent_config_text": "",
    "user_config_text": "",
    "settings_overrides": {},
    "is_settings_narrowing_allowed": False,
    "previously_rendered_model_settings": {},
}

_STUB_MODEL_TABLE: Final[dict[str, Any]] = {
    "model": "stub-model",
    "base_url": "http://127.0.0.1:18746/v1",
    "api_key": "stub-key",
    "name": "Stub",
    "context_window": 131072,
    "reasoning_efforts": [{"value": "low"}, {"value": "high"}],
}


def _render_config(**changed_inputs: Any) -> RenderedGrokConfig:
    return build_config_toml(**{**_DEFAULT_CONFIG_INPUTS, **changed_inputs})


def _build_config(**changed_inputs: Any) -> dict[str, Any]:
    return _render_config(**changed_inputs).body


def _read_fixture_toml(filename: str) -> dict[str, Any]:
    return tomlkit.parse((CONFIG_FIXTURES_DIR / filename).read_text()).unwrap()


def _answer_toml(notice_id: str, version: int, account: str) -> str:
    return f'[consent.answers.{notice_id}]\nversion = {version}\naccount = "{account}"\nacked = true\n'


def test_build_config_toml_default_agent_renders_every_pin_and_full_isolation() -> None:
    all_cells_off = {cell: False for cell in COMPAT_CELL_NAMES}

    config = _build_config()

    assert config == {
        "cli": {"auto_update": False, "use_leader": False, "show_tips": False},
        "features": {"telemetry": False, "feedback": False, "campaigns": False, "remote_fetch": True},
        "telemetry": {"trace_upload": False, "mixpanel_enabled": False},
        "harness": {"disable_workspace_teleport": True, "disable_codebase_upload": True},
        "ui": {
            "permission_mode": "ask",
            "prompt_suggestions": False,
            "cancel_subagents_on_turn_cancel": "always_continue",
        },
        "privacy": {"privacy_banner_acked": "2026-10-19T18:00:00Z"},
        "memory": {"enabled": False},
        "claude_compat": {"imported": True},
        "compat": {"claude": all_cells_off, "cursor": all_cells_off, "codex": {"sessions": False}},
    }


def test_build_config_toml_matches_the_config_grok_accepted_with_only_its_own_two_warnings() -> None:
    config = _build_config(
        model="stub",
        reasoning_effort="high",
        settings_overrides={
            "model": {"stub": _STUB_MODEL_TABLE},
            "ui__extend": {"status_line": {"type": "command", "command": "/fixture/statusline_silent.sh"}},
        },
    )

    rendered = tomlkit.parse(render_toml(config)).unwrap()
    assert rendered.pop("memory") == {"enabled": False}
    assert rendered == _read_fixture_toml("config_full_isolation.toml")


def test_build_config_toml_auto_allow_permissions_selects_always_approve() -> None:
    config = _build_config(is_auto_allow_permissions=True)

    assert config["ui"]["permission_mode"] == "always-approve"


def test_build_config_toml_leaves_the_updater_alone_when_self_update_is_allowed() -> None:
    config = _build_config(is_self_update_disabled=False)

    assert config["cli"] == {"use_leader": False, "show_tips": False}


def test_build_config_toml_without_privacy_pins_keeps_only_the_non_privacy_keys() -> None:
    config = _build_config(is_privacy_pinned=False)

    assert config["features"] == {"remote_fetch": True}
    assert "telemetry" not in config
    assert "harness" not in config


def test_build_config_toml_writes_memory_enabled_when_memory_is_on() -> None:
    config = _build_config(is_memory_enabled=True)

    assert config["memory"] == {"enabled": True}


def test_build_config_toml_privacy_banner_acked_is_rendered_in_utc() -> None:
    acked_at = datetime(2026, 10, 19, 20, 30, 15, tzinfo=timezone(timedelta(hours=2)))

    config = _build_config(privacy_banner_acked_at=acked_at)

    assert config["privacy"] == {"privacy_banner_acked": "2026-10-19T18:30:15Z"}


def test_build_config_toml_configured_model_and_effort_win_over_the_existing_file() -> None:
    existing = '[models]\ndefault = "grok-old"\ndefault_reasoning_effort = "low"\n'

    config = _build_config(model="grok-new", reasoning_effort="high", existing_agent_config_text=existing)

    assert config["models"] == {"default": "grok-new", "default_reasoning_effort": "high"}


def test_build_config_toml_carries_over_the_model_choice_made_in_the_agent_when_unset() -> None:
    existing = '[models]\ndefault = "grok-picked"\ndefault_reasoning_effort = "medium"\n[ui]\ncompact_mode = true\n'

    config = _build_config(existing_agent_config_text=existing)

    assert config["models"] == {"default": "grok-picked", "default_reasoning_effort": "medium"}
    assert config["ui"] == {
        "permission_mode": "ask",
        "prompt_suggestions": False,
        "cancel_subagents_on_turn_cancel": "always_continue",
    }


def test_build_config_toml_carries_over_each_model_setting_independently() -> None:
    existing = '[models]\ndefault_reasoning_effort = "medium"\n'

    config = _build_config(model="grok-configured", existing_agent_config_text=existing)

    assert config["models"] == {"default": "grok-configured", "default_reasoning_effort": "medium"}


def test_build_config_toml_carries_over_the_model_settings_a_bare_models_override_leaves_unset() -> None:
    existing = '[models]\ndefault = "grok-picked"\ndefault_reasoning_effort = "medium"\n'

    config = _build_config(settings_overrides={"models": {"default": "stub"}}, existing_agent_config_text=existing)

    assert config["models"] == {"default": "stub", "default_reasoning_effort": "medium"}


def test_build_config_toml_drops_the_model_settings_it_rendered_once_they_are_unset() -> None:
    existing = '[models]\ndefault = "grok-old"\ndefault_reasoning_effort = "low"\n'

    config = _build_config(
        existing_agent_config_text=existing,
        previously_rendered_model_settings={"default": "grok-old", "default_reasoning_effort": "low"},
    )

    assert "models" not in config


def test_build_config_toml_carries_over_a_model_choice_grok_changed_since_the_last_provision() -> None:
    existing = '[models]\ndefault = "grok-picked"\ndefault_reasoning_effort = "low"\n'

    config = _build_config(
        existing_agent_config_text=existing,
        previously_rendered_model_settings={"default": "grok-old", "default_reasoning_effort": "low"},
    )

    assert config["models"] == {"default": "grok-picked"}


def test_build_config_toml_own_model_settings_leave_out_carried_over_choices() -> None:
    existing = '[models]\ndefault = "grok-picked"\ndefault_reasoning_effort = "medium"\n'

    rendered = _render_config(settings_overrides={"models": {"default": "stub"}}, existing_agent_config_text=existing)

    assert rendered.body["models"] == {"default": "stub", "default_reasoning_effort": "medium"}
    assert rendered.own_model_settings == {"default": "stub"}


def test_rendered_model_settings_round_trip_and_blank_text_is_empty() -> None:
    settings = {"default_reasoning_effort": "high", "default": "grok-4.6"}

    assert parse_rendered_model_settings(render_rendered_model_settings(settings), "record") == settings
    assert parse_rendered_model_settings("\n", "record") == {}


@pytest.mark.parametrize("text", ["{not json", '["grok-4.6"]', '{"default": 3}'])
def test_parse_rendered_model_settings_rejects_a_malformed_record(text: str) -> None:
    with pytest.raises(MalformedGrokConfigError, match="the record .*remove it, then re-run"):
        parse_rendered_model_settings(text, "the record")


def test_build_config_toml_omits_models_when_nothing_is_configured_or_carried_over() -> None:
    config = _build_config(existing_agent_config_text="[ui]\nyolo = false\n")

    assert "models" not in config


def test_build_config_toml_rejects_a_non_string_carried_over_model() -> None:
    with pytest.raises(MalformedGrokConfigError, match=r"\[models\] default"):
        _build_config(existing_agent_config_text="[models]\ndefault = 3\n")


def test_build_config_toml_rejects_an_existing_models_value_that_is_not_a_table() -> None:
    with pytest.raises(MalformedGrokConfigError, match=r"\[models\] value that is not a table"):
        _build_config(existing_agent_config_text='models = "grok-4.7"\n')


def test_build_config_toml_refuses_to_overwrite_a_malformed_existing_file() -> None:
    with pytest.raises(MalformedGrokConfigError, match="The agent's grok config.toml contains malformed TOML"):
        _build_config(existing_agent_config_text="[models\n")


@pytest.mark.parametrize(
    ("settings_overrides", "expected_model", "is_remote_fetch_expected"),
    [
        *(
            pytest.param(
                {"model": {"custom": {"model": "m", credential_key: "value"}}},
                {"custom": {"model": "m", credential_key: "value"}},
                False,
                id=credential_key,
            )
            for credential_key in ("base_url", "api_key", "env_key")
        ),
        pytest.param(
            {"model__extend": {"custom": {"base_url": "http://localhost:9/v1"}}},
            {"custom": {"base_url": "http://localhost:9/v1"}},
            False,
            id="credential_given_as_an_extend",
        ),
        pytest.param(
            {"model": {"alias": {"model": "grok-4.7", "name": "Alias"}}},
            {"alias": {"model": "grok-4.7", "name": "Alias"}},
            True,
            id="model_table_without_a_credential",
        ),
        pytest.param({"model": "grok-4.7"}, "grok-4.7", True, id="model_override_not_a_table"),
    ],
)
def test_build_config_toml_turns_remote_fetch_off_only_for_an_override_model_with_its_own_credential(
    settings_overrides: dict[str, Any], expected_model: object, is_remote_fetch_expected: bool
) -> None:
    config = _build_config(settings_overrides=settings_overrides)

    assert (config["features"]["remote_fetch"], config["model"]) == (is_remote_fetch_expected, expected_model)


def test_build_config_toml_isolation_disables_each_named_subagent_once() -> None:
    config = _build_config(
        subagent_names_to_disable=("zeta-agent", "alpha.agent", "zeta-agent"),
        enabled_claude_plugin_keys=("ignored-plugin@market",),
    )

    assert config["subagents"] == {"toggle": {"alpha.agent": False, "zeta-agent": False}}
    assert "plugins" not in config


def test_build_config_toml_opt_out_keeps_claude_surfaces_but_turns_hooks_and_plugins_off() -> None:
    config = _build_config(
        is_isolated_from_claude_config=False,
        subagent_names_to_disable=("ignored-agent",),
        enabled_claude_plugin_keys=("probe-plugin@probe-mkt", "probe-plugin@other-mkt", "bare-plugin"),
    )

    assert config["compat"] == {"claude": {"hooks": False}, "cursor": {"hooks": False}}
    assert config["plugins"] == {"disabled": ["bare-plugin", "probe-plugin"]}
    assert "claude_compat" not in config
    assert "subagents" not in config


def test_build_config_toml_opt_out_without_plugins_writes_no_plugins_table() -> None:
    config = _build_config(is_isolated_from_claude_config=False)

    assert "plugins" not in config


def test_strip_claude_plugin_marketplace_suffixes_keeps_the_name_before_the_last_at_sign() -> None:
    names = strip_claude_plugin_marketplace_suffixes(("a@m1", "@scope/b@m2", "@scope/c", "a@m3"))

    assert names == ["@scope/b", "@scope/c", "a"]


def test_build_config_toml_extend_override_merges_onto_mngr_pins() -> None:
    config = _build_config(settings_overrides={"ui__extend": {"theme": "dark"}})

    assert config["ui"] == {
        "permission_mode": "ask",
        "prompt_suggestions": False,
        "cancel_subagents_on_turn_cancel": "always_continue",
        "theme": "dark",
    }


def test_build_config_toml_bare_override_that_drops_mngr_pins_is_a_narrowing_error() -> None:
    with pytest.raises(ConfigParseError, match="narrowing"):
        _build_config(settings_overrides={"ui": {"theme": "dark"}})


def test_build_config_toml_bare_override_replaces_the_table_when_narrowing_is_allowed() -> None:
    config = _build_config(settings_overrides={"ui": {"theme": "dark"}}, is_settings_narrowing_allowed=True)

    assert config["ui"] == {"theme": "dark"}


def test_build_config_toml_override_of_a_pinned_scalar_wins() -> None:
    config = _build_config(settings_overrides={"memory": {"enabled": True}})

    assert config["memory"] == {"enabled": True}


def test_build_config_toml_includes_consent_answers_from_the_shared_credential_side() -> None:
    user_config = (CONFIG_FIXTURES_DIR / "config_after_consent_accept.toml").read_text()

    config = _build_config(user_config_text=user_config)

    assert config["consent"] == {
        "answers": {"stub-consent": {"version": 1, "account": "stub-user@test.invalid", "acked": True}}
    }


def test_merge_consent_answers_keeps_answers_found_on_only_one_side() -> None:
    merged = merge_consent_answers(_answer_toml("agent-only", 2, "a@x"), _answer_toml("user-only", 5, "a@x"))

    assert merged == {
        "agent-only": {"version": 2, "account": "a@x", "acked": True},
        "user-only": {"version": 5, "account": "a@x", "acked": True},
    }


def test_merge_consent_answers_same_account_keeps_the_higher_version() -> None:
    merged_agent_newer = merge_consent_answers(_answer_toml("n", 3, "a@x"), _answer_toml("n", 2, "a@x"))
    merged_user_newer = merge_consent_answers(_answer_toml("n", 1, "a@x"), _answer_toml("n", 2, "a@x"))

    assert merged_agent_newer["n"]["version"] == 3
    assert merged_user_newer["n"]["version"] == 2


def test_merge_consent_answers_same_account_tie_keeps_the_credential_side_answer() -> None:
    agent_answer = '[consent.answers.n]\nversion = 2\naccount = "a@x"\nacked = false\n'

    merged = merge_consent_answers(agent_answer, _answer_toml("n", 2, "a@x"))

    assert merged["n"] == {"version": 2, "account": "a@x", "acked": True}


def test_merge_consent_answers_different_accounts_keep_the_credential_side_answer() -> None:
    merged = merge_consent_answers(_answer_toml("n", 9, "old@x"), _answer_toml("n", 1, "new@x"))

    assert merged["n"] == {"version": 1, "account": "new@x", "acked": True}


def test_merge_consent_answers_of_two_empty_files_is_empty() -> None:
    assert merge_consent_answers("", "  \n") == {}


@pytest.mark.parametrize(
    ("user_config_text", "message"),
    [
        ("[consent\n", "malformed TOML"),
        ("consent = 1\n", r"\[consent\] value"),
        ("[consent]\nanswers = 1\n", r"\[consent.answers\] value"),
        ('[consent.answers]\nn = "yes"\n', r"\[consent.answers.n\] that is not a table"),
        ('[consent.answers.n]\nversion = "1"\naccount = "a@x"\n', "version is not an integer"),
        ('[consent.answers.n]\nversion = true\naccount = "a@x"\n', "version is not an integer"),
        ("[consent.answers.n]\nversion = 1\n", "account is not a string"),
    ],
)
def test_merge_consent_answers_rejects_an_unexpected_answer_shape(user_config_text: str, message: str) -> None:
    with pytest.raises(MalformedGrokConfigError, match=message):
        merge_consent_answers("", user_config_text)


def test_parse_grok_config_text_of_blank_text_is_empty() -> None:
    assert parse_grok_config_text(" \n\t\n", "a blank file") == {}


def test_render_toml_round_trips_nested_tables_and_quotes_dotted_keys() -> None:
    body = {"subagents": {"toggle": {"my.agent": False}}, "consent": {"answers": {"n-1": {"version": 1}}}}

    rendered = render_toml(body)

    assert '"my.agent" = false' in rendered
    assert tomlkit.parse(rendered).unwrap() == body


def test_build_requirements_toml_matches_the_pins_that_survived_a_session() -> None:
    rendered = build_requirements_toml(is_privacy_pinned=True)

    assert tomlkit.parse(rendered).unwrap() == _read_fixture_toml("requirements.toml")


def test_build_requirements_toml_is_empty_without_privacy_pins() -> None:
    assert build_requirements_toml(is_privacy_pinned=False) == ""


def test_build_hooks_json_uses_the_entry_shape_grok_ran_directly() -> None:
    fixture = json.loads((CONFIG_FIXTURES_DIR / "hooks_mngr.json").read_text())

    hooks_json = build_hooks_json()

    assert set(hooks_json["hooks"]) <= set(fixture["hooks"])
    for event_name, entries in hooks_json["hooks"].items():
        assert entries == fixture["hooks"][event_name]


def test_registered_hook_events_are_exactly_the_events_the_handler_acts_on() -> None:
    registered_snake_case_names = {grok_hook.snake_case_event_name(name) for name in GROK_HOOK_EVENT_NAMES}

    assert registered_snake_case_names == set(grok_hook.HOOK_EVENT_HANDLERS)


def _load_session_bucket_cases() -> list[dict[str, Any]]:
    return json.loads((CONFIG_FIXTURES_DIR / "session_buckets.json").read_text())["cases"]


@pytest.mark.parametrize(
    "case",
    [case for case in _load_session_bucket_cases() if not case["is_long_form"]],
    ids=lambda case: case["label"],
)
def test_compute_session_bucket_name_reproduces_each_bucket_grok_created(case: dict[str, Any]) -> None:
    assert compute_session_bucket_name(case["canonical_path"]) == case["observed_bucket"]


@pytest.mark.parametrize(
    "case",
    [case for case in _load_session_bucket_cases() if case["is_long_form"]],
    ids=lambda case: case["label"],
)
def test_compute_session_bucket_name_rejects_paths_grok_stores_under_a_hashed_name(case: dict[str, Any]) -> None:
    with pytest.raises(SessionBucketNameError, match="over 255"):
        compute_session_bucket_name(case["canonical_path"])


def test_session_bucket_fixture_covers_fifteen_cases_with_both_sides_of_the_boundary() -> None:
    cases_by_label = {case["label"]: case for case in _load_session_bucket_cases()}

    assert len(cases_by_label) == 15
    assert len(cases_by_label["ascii_255"]["observed_bucket"]) == 255
    assert cases_by_label["ascii_256"]["is_long_form"] is True


def test_compute_session_bucket_name_rejects_a_relative_path() -> None:
    with pytest.raises(SessionBucketNameError, match="absolute"):
        compute_session_bucket_name("relative/work")


def test_build_pinned_grok_environment_under_isolation_turns_every_compat_surface_off() -> None:
    environment = build_pinned_grok_environment(
        grok_home=Path("/state/plugin/grok/home"), is_privacy_pinned=True, is_isolated_from_claude_config=True
    )

    assert environment == {
        "GROK_HOME": "/state/plugin/grok/home",
        "GROK_MANAGED_CONFIG": "0",
        "GROK_CHANGELOG_OFFLINE": "1",
        "GROK_CLAUDE_SKILLS_ENABLED": "0",
        "GROK_CLAUDE_RULES_ENABLED": "0",
        "GROK_CLAUDE_AGENTS_ENABLED": "0",
        "GROK_CLAUDE_MCPS_ENABLED": "0",
        "GROK_CLAUDE_HOOKS_ENABLED": "0",
        "GROK_CLAUDE_SESSIONS_ENABLED": "0",
        "GROK_CURSOR_SKILLS_ENABLED": "0",
        "GROK_CURSOR_RULES_ENABLED": "0",
        "GROK_CURSOR_AGENTS_ENABLED": "0",
        "GROK_CURSOR_MCPS_ENABLED": "0",
        "GROK_CURSOR_HOOKS_ENABLED": "0",
        "GROK_CURSOR_SESSIONS_ENABLED": "0",
        "GROK_CODEX_SESSIONS_ENABLED": "0",
        "DISABLE_TELEMETRY": "1",
        "GROK_TELEMETRY_TRACE_UPLOAD": "0",
        "GROK_CAMPAIGNS": "0",
    }


def test_build_pinned_grok_environment_opt_out_without_privacy_pins_turns_only_hooks_off() -> None:
    environment = build_pinned_grok_environment(
        grok_home=Path("/g"), is_privacy_pinned=False, is_isolated_from_claude_config=False
    )

    assert environment == {
        "GROK_HOME": "/g",
        "GROK_MANAGED_CONFIG": "0",
        "GROK_CHANGELOG_OFFLINE": "1",
        "GROK_CLAUDE_HOOKS_ENABLED": "0",
        "GROK_CURSOR_HOOKS_ENABLED": "0",
    }


def test_build_removed_grok_environment_names_removes_the_auth_path_only_when_auth_is_not_shared() -> None:
    shared_names = build_removed_grok_environment_names(is_auth_shared=True)
    unshared_names = build_removed_grok_environment_names(is_auth_shared=False)

    assert shared_names == ("GROK_AGENT", "GROK_CONFIG", "GROK_CONFIG_PATH", "GROK_FOLDER_TRUST", "CLAUDECODE")
    assert unshared_names == shared_names + ("GROK_AUTH_PATH",)


def test_build_launch_command_runs_the_launch_script_with_the_grok_words() -> None:
    command = build_launch_command(
        command=CommandString("grok"),
        cli_args=("--no-plan",),
        agent_args=("--model", "Grok 4 (fast)", 'it\'s "quoted"'),
        command_override=None,
    )

    assert "\n" not in command
    assert shlex.split(command) == [
        "bash",
        "$MNGR_AGENT_STATE_DIR/commands/grok_launch.sh",
        "grok",
        "--no-plan",
        "--model",
        "Grok 4 (fast)",
        'it\'s "quoted"',
    ]


def test_build_launch_command_keeps_a_launcher_prefix_and_shell_safe_cli_args_as_written() -> None:
    command = build_launch_command(
        command=CommandString("env FOO=1 grok"),
        cli_args=("--allow", "'Bash(ls *)'"),
        agent_args=(),
        command_override=None,
    )

    assert command == CommandString(
        "bash \"$MNGR_AGENT_STATE_DIR/commands/grok_launch.sh\" env FOO=1 grok --allow 'Bash(ls *)'"
    )


def test_build_launch_command_uses_the_command_override_as_the_base() -> None:
    command = build_launch_command(
        command=CommandString("grok"),
        cli_args=(),
        agent_args=("x",),
        command_override=CommandString("/opt/grok-dev/bin/grok"),
    )

    assert shlex.split(command)[2:] == ["/opt/grok-dev/bin/grok", "x"]


def test_build_launch_env_renders_shell_quoted_assignments_and_arrays() -> None:
    launch_env = build_launch_env(
        grok_home=Path("/state dir/plugin/grok/home"),
        work_dir=Path("/work/it's here"),
        shared_user_auth_path=Path("/home/u/.grok/auth.json"),
        model="grok-4.7",
        reasoning_effort=None,
        is_privacy_pinned=False,
        is_isolated_from_claude_config=False,
    )

    assert launch_env.splitlines() == [
        "# Rendered by mngr at provision; sourced by commands/grok_launch.sh.",
        "launch_grok_home='/state dir/plugin/grok/home'",
        "launch_work_dir='/work/it'\"'\"'s here'",
        "launch_is_auth_shared=true",
        "launch_user_auth_path=/home/u/.grok/auth.json",
        "launch_model=grok-4.7",
        "launch_reasoning_effort=''",
        "launch_pinned_environment=('GROK_HOME=/state dir/plugin/grok/home' GROK_MANAGED_CONFIG=0 "
        "GROK_CHANGELOG_OFFLINE=1 GROK_CLAUDE_HOOKS_ENABLED=0 GROK_CURSOR_HOOKS_ENABLED=0)",
        "launch_removed_environment=(GROK_AGENT GROK_CONFIG GROK_CONFIG_PATH GROK_FOLDER_TRUST CLAUDECODE)",
    ]


def test_build_launch_env_without_shared_auth_removes_the_inherited_auth_path() -> None:
    launch_env = build_launch_env(
        grok_home=Path("/g"),
        work_dir=Path("/w"),
        shared_user_auth_path=None,
        model=None,
        reasoning_effort="high",
        is_privacy_pinned=True,
        is_isolated_from_claude_config=True,
    )

    lines = launch_env.splitlines()
    assert "launch_is_auth_shared=false" in lines
    assert "launch_user_auth_path=''" in lines
    assert "launch_reasoning_effort=high" in lines
    assert lines[-1].endswith(" CLAUDECODE GROK_AUTH_PATH)")


def test_get_grok_home_is_under_the_plugin_state_dir() -> None:
    assert get_grok_home(Path("/hosts/agents/agent-1")) == Path("/hosts/agents/agent-1/plugin/grok/home")


@pytest.mark.parametrize(
    ("turn_text", "expected_prompt_id", "is_pending_wake"),
    [
        ("0b1c-prompt\nfixthetest\n", "0b1c-prompt", False),
        (f"task-completed-7\n{grok_hook.AUTO_WAKE_TAIL_MARKER}\n", "task-completed-7", False),
        (
            f"{grok_hook.PENDING_WAKE_PROMPT_ID_PREFIX}child-1\n{grok_hook.AUTO_WAKE_TAIL_MARKER}\n",
            "pending-wake:child-1",
            True,
        ),
        ("", "", False),
    ],
)
def test_parse_root_turn_reads_the_prompt_id_and_the_pending_wake_placeholder(
    turn_text: str, expected_prompt_id: str, is_pending_wake: bool
) -> None:
    assert parse_root_turn("root-1", turn_text) == GrokRootTurn(
        session_id="root-1", prompt_id=expected_prompt_id, is_pending_wake=is_pending_wake
    )
