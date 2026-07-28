import tomllib
from pathlib import Path

from imbue.minds.desktop_client.laptop_agent_types_seed import seed_laptop_agent_types_for_minds
from imbue.mngr.config.agent_class_registry import get_agent_class
from imbue.mngr.config.agent_class_registry import is_agent_class_registered
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.loader import get_or_create_profile_dir
from imbue.mngr.config.loader import parse_config
from imbue.mngr.primitives import AgentTypeName

# The agent types the DEFAULT_WORKSPACE_TEMPLATE workspace creates agents with;
# each must resolve laptop-side (where the workspace's own `.mngr/settings.toml`
# is never loaded).
_WORKSPACE_AGENT_TYPES = ("chat", "main", "worker")


def _read_seeded_settings_text(host_dir: Path) -> str:
    return (get_or_create_profile_dir(host_dir) / "settings.toml").read_text()


def test_every_workspace_agent_type_resolves_to_the_claude_agent_class(temp_host_dir: Path) -> None:
    """The seeded config must resolve each workspace type to claude's agent class.

    This is the property the latchkey permission-approval nudge depends on: a
    laptop-side ``mngr message`` to a workspace chat agent only reaches Claude's
    TUI when the agent's stored type resolves to the claude agent class, rather
    than degrading to the send_message-less orphan fallback.
    """
    assert is_agent_class_registered("claude"), "the imbue-mngr-claude plugin must be installed for this test"
    seed_laptop_agent_types_for_minds(temp_host_dir)

    config = parse_config(tomllib.loads(_read_seeded_settings_text(temp_host_dir)), disabled_plugins=frozenset())

    for type_name in _WORKSPACE_AGENT_TYPES:
        resolved = resolve_agent_type(AgentTypeName(type_name), config)
        assert resolved.agent_class is get_agent_class("claude")


def test_seeding_is_idempotent_across_launches(temp_host_dir: Path) -> None:
    """Re-seeding on every startup must not re-append blocks already present."""
    seed_laptop_agent_types_for_minds(temp_host_dir)
    after_first_seed = _read_seeded_settings_text(temp_host_dir)

    seed_laptop_agent_types_for_minds(temp_host_dir)

    assert _read_seeded_settings_text(temp_host_dir) == after_first_seed


def test_types_missing_from_an_older_seeded_file_are_appended(temp_host_dir: Path) -> None:
    """A settings.toml seeded by an older build (only `main`) gains the newer types.

    The pre-existing block is left untouched (its hand-set fields survive) and
    the file still parses, with every workspace type resolvable.
    """
    settings_path = get_or_create_profile_dir(temp_host_dir) / "settings.toml"
    settings_path.write_text(
        'is_allowed_in_pytest = true\n\n[agent_types.main]\nparent_type = "claude"\nsync_claude_json = false\n'
    )

    seed_laptop_agent_types_for_minds(temp_host_dir)

    raw = tomllib.loads(settings_path.read_text())
    assert raw["agent_types"]["main"]["sync_claude_json"] is False
    config = parse_config(raw, disabled_plugins=frozenset())
    for type_name in _WORKSPACE_AGENT_TYPES:
        assert resolve_agent_type(AgentTypeName(type_name), config).agent_class is get_agent_class("claude")


def test_seeded_file_is_readable_by_mngr_under_pytest(temp_host_dir: Path) -> None:
    """Under pytest the seed must opt the file in, as a top-level key (before any section)."""
    seed_laptop_agent_types_for_minds(temp_host_dir)

    text = _read_seeded_settings_text(temp_host_dir)
    assert tomllib.loads(text)["is_allowed_in_pytest"] is True
    assert text.startswith("is_allowed_in_pytest = true\n")
