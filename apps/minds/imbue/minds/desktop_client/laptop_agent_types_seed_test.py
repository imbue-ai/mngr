import tomllib
from pathlib import Path

from imbue.minds.desktop_client.laptop_agent_types_seed import seed_laptop_agent_types_for_minds
from imbue.mngr.config.agent_class_registry import get_agent_class
from imbue.mngr.config.agent_class_registry import is_agent_class_registered
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.loader import get_or_create_profile_dir
from imbue.mngr.config.loader import parse_config
from imbue.mngr.primitives import AgentTypeName

# The agent types the DEFAULT_WORKSPACE_TEMPLATE workspace creates agents with,
# each paired with the registered type whose agent class it must resolve to
# laptop-side (where the workspace's own `.mngr/settings.toml` is never loaded).
# `main` is a plain command agent since the workspace's ~/.claude cutover; the
# claude-binary types stay claude-parented.
_WORKSPACE_AGENT_TYPE_EXPECTED_PARENTS = (("chat", "claude"), ("main", "command"), ("worker", "claude"))


def _read_seeded_settings_text(host_dir: Path) -> str:
    return (get_or_create_profile_dir(host_dir) / "settings.toml").read_text()


def test_every_workspace_agent_type_resolves_to_its_workspace_agent_class(temp_host_dir: Path) -> None:
    """The seeded config must resolve each workspace type to the right agent class.

    Chat/worker resolving to claude's agent class is the property the latchkey
    permission-approval nudge depends on: a laptop-side ``mngr message`` to a
    workspace chat agent only reaches Claude's TUI when the agent's stored type
    resolves to the claude agent class, rather than degrading to the
    send_message-less orphan fallback. `main` must resolve to the plain command
    agent, matching the workspace template's own declaration -- a mismatched
    parent would make the user-scope entry unmergeable with the workspace
    repo's `[agent_types.main]` at create time.
    """
    assert is_agent_class_registered("claude"), "the imbue-mngr-claude plugin must be installed for this test"
    seed_laptop_agent_types_for_minds(temp_host_dir)

    config = parse_config(tomllib.loads(_read_seeded_settings_text(temp_host_dir)), disabled_plugins=frozenset())

    for type_name, expected_parent in _WORKSPACE_AGENT_TYPE_EXPECTED_PARENTS:
        resolved = resolve_agent_type(AgentTypeName(type_name), config)
        assert resolved.agent_class is get_agent_class(expected_parent)


def test_seeding_is_idempotent_across_launches(temp_host_dir: Path) -> None:
    """Re-seeding on every startup must not re-append blocks already present."""
    seed_laptop_agent_types_for_minds(temp_host_dir)
    after_first_seed = _read_seeded_settings_text(temp_host_dir)

    seed_laptop_agent_types_for_minds(temp_host_dir)

    assert _read_seeded_settings_text(temp_host_dir) == after_first_seed


def test_stale_claude_parented_main_seed_is_migrated_to_command(temp_host_dir: Path) -> None:
    """A `main` block seeded by a pre-cutover build is rewritten in place.

    The laptop profile outlives workspace re-creation, and a claude-parented
    user-scope `main` cross-scope-merges against the new workspace repo's
    command-parented `main` at create time, which mngr rejects ("Cannot merge
    AgentTypeConfig with ClaudeAgentConfig"). The exact seeder-written block
    must therefore be migrated, missing types appended, and every type must
    resolve to its current workspace agent class.
    """
    settings_path = get_or_create_profile_dir(temp_host_dir) / "settings.toml"
    settings_path.write_text('is_allowed_in_pytest = true\n\n[agent_types.main]\nparent_type = "claude"\n')

    seed_laptop_agent_types_for_minds(temp_host_dir)

    raw = tomllib.loads(settings_path.read_text())
    assert raw["agent_types"]["main"]["parent_type"] == "command"
    config = parse_config(raw, disabled_plugins=frozenset())
    for type_name, expected_parent in _WORKSPACE_AGENT_TYPE_EXPECTED_PARENTS:
        assert resolve_agent_type(AgentTypeName(type_name), config).agent_class is get_agent_class(expected_parent)


def test_hand_edited_main_block_is_left_untouched(temp_host_dir: Path) -> None:
    """A `main` block with hand-set extra fields is NOT rewritten.

    Silently flipping the parent under hand-set claude-only fields would
    produce a config that fails validation in a different, more confusing
    place; the deliberate choice is to leave it alone so the cross-scope merge
    error surfaces for the user to resolve.
    """
    settings_path = get_or_create_profile_dir(temp_host_dir) / "settings.toml"
    hand_edited = (
        'is_allowed_in_pytest = true\n\n[agent_types.main]\nparent_type = "claude"\nsync_claude_json = false\n'
    )
    settings_path.write_text(hand_edited)

    seed_laptop_agent_types_for_minds(temp_host_dir)

    raw = tomllib.loads(settings_path.read_text())
    assert raw["agent_types"]["main"]["parent_type"] == "claude"
    assert raw["agent_types"]["main"]["sync_claude_json"] is False


def test_seeded_file_is_readable_by_mngr_under_pytest(temp_host_dir: Path) -> None:
    """Under pytest the seed must opt the file in, as a top-level key (before any section)."""
    seed_laptop_agent_types_for_minds(temp_host_dir)

    text = _read_seeded_settings_text(temp_host_dir)
    assert tomllib.loads(text)["is_allowed_in_pytest"] is True
    assert text.startswith("is_allowed_in_pytest = true\n")
