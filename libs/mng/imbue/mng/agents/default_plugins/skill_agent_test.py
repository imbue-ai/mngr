"""Tests for skill-provisioned agent types (code-guardian, fixme-fairy)."""

from pathlib import Path

import pluggy
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.agents.agent_registry import list_registered_agent_types
from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgent
from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgentConfig
from imbue.mng.agents.default_plugins.code_guardian_agent import CodeGuardianAgent
from imbue.mng.agents.default_plugins.code_guardian_agent import CodeGuardianAgentConfig
from imbue.mng.agents.default_plugins.code_guardian_agent import _CODE_GUARDIAN_SKILL_CONTENT
from imbue.mng.agents.default_plugins.code_guardian_agent import _SKILL_NAME as CODE_GUARDIAN_SKILL_NAME
from imbue.mng.agents.default_plugins.fixme_fairy_agent import FixmeFairyAgent
from imbue.mng.agents.default_plugins.fixme_fairy_agent import FixmeFairyAgentConfig
from imbue.mng.agents.default_plugins.fixme_fairy_agent import _FIXME_FAIRY_SKILL_CONTENT
from imbue.mng.agents.default_plugins.fixme_fairy_agent import _SKILL_NAME as FIXME_FAIRY_SKILL_NAME
from imbue.mng.agents.default_plugins.skill_agent import SkillProvisionedAgent
from imbue.mng.agents.default_plugins.skill_agent import _install_skill_locally
from imbue.mng.config.agent_class_registry import get_agent_class
from imbue.mng.config.agent_config_registry import get_agent_config_class
from imbue.mng.config.agent_config_registry import resolve_agent_type
from imbue.mng.config.data_types import MngConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.conftest import make_mng_ctx
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString

# Each tuple: (type_name, agent_class, config_class, skill_name, skill_content)
_SKILL_AGENTS = [
    pytest.param(
        "code-guardian",
        CodeGuardianAgent,
        CodeGuardianAgentConfig,
        CODE_GUARDIAN_SKILL_NAME,
        _CODE_GUARDIAN_SKILL_CONTENT,
        id="code-guardian",
    ),
    pytest.param(
        "fixme-fairy",
        FixmeFairyAgent,
        FixmeFairyAgentConfig,
        FIXME_FAIRY_SKILL_NAME,
        _FIXME_FAIRY_SKILL_CONTENT,
        id="fixme-fairy",
    ),
]

# Just skill name + content for install tests
_SKILL_CONTENTS = [
    pytest.param(CODE_GUARDIAN_SKILL_NAME, _CODE_GUARDIAN_SKILL_CONTENT, id="code-guardian"),
    pytest.param(FIXME_FAIRY_SKILL_NAME, _FIXME_FAIRY_SKILL_CONTENT, id="fixme-fairy"),
]


# ── Registration tests ──────────────────────────────────────────────────


@pytest.mark.parametrize("type_name,agent_class,config_class,skill_name,skill_content", _SKILL_AGENTS)
def test_skill_agent_is_registered_in_agent_types(
    type_name: str,
    agent_class: type,
    config_class: type,
    skill_name: str,
    skill_content: str,
) -> None:
    """Skill-provisioned agents should appear in the list of registered agent types."""
    agent_types = list_registered_agent_types()
    assert type_name in agent_types


@pytest.mark.parametrize("type_name,agent_class,config_class,skill_name,skill_content", _SKILL_AGENTS)
def test_skill_agent_class_is_correct(
    type_name: str,
    agent_class: type,
    config_class: type,
    skill_name: str,
    skill_content: str,
) -> None:
    """Each skill agent type should return the correct agent class."""
    assert get_agent_class(type_name) == agent_class


@pytest.mark.parametrize("type_name,agent_class,config_class,skill_name,skill_content", _SKILL_AGENTS)
def test_skill_agent_config_class_is_correct(
    type_name: str,
    agent_class: type,
    config_class: type,
    skill_name: str,
    skill_content: str,
) -> None:
    """Each skill agent type should return the correct config class."""
    assert get_agent_config_class(type_name) == config_class


# ── Config inheritance tests ─────────────────────────────────────────────


@pytest.mark.parametrize("type_name,agent_class,config_class,skill_name,skill_content", _SKILL_AGENTS)
def test_skill_agent_config_inherits_claude_defaults(
    type_name: str,
    agent_class: type,
    config_class: type,
    skill_name: str,
    skill_content: str,
) -> None:
    """Skill agent configs should have the same defaults as ClaudeAgentConfig."""
    config = config_class()
    assert config.command == CommandString("claude")
    assert config.sync_home_settings is True
    assert config.check_installation is True


@pytest.mark.parametrize("type_name,agent_class,config_class,skill_name,skill_content", _SKILL_AGENTS)
def test_skill_agent_config_inherits_claude_cli_args(
    type_name: str,
    agent_class: type,
    config_class: type,
    skill_name: str,
    skill_content: str,
) -> None:
    """Skill agent configs should inherit ClaudeAgentConfig's default cli_args."""
    config = config_class()
    assert config.cli_args == ClaudeAgentConfig().cli_args


@pytest.mark.parametrize("type_name,agent_class,config_class,skill_name,skill_content", _SKILL_AGENTS)
def test_skill_agent_config_has_no_custom_cli_args(
    type_name: str,
    agent_class: type,
    config_class: type,
    skill_name: str,
    skill_content: str,
) -> None:
    """Skill agent configs should not add any custom cli_args beyond ClaudeAgentConfig."""
    config = config_class()
    assert config.cli_args == ()


# ── Type resolution tests ────────────────────────────────────────────────


@pytest.mark.parametrize("type_name,agent_class,config_class,skill_name,skill_content", _SKILL_AGENTS)
def test_resolve_skill_agent_type_returns_correct_agent_and_config(
    type_name: str,
    agent_class: type,
    config_class: type,
    skill_name: str,
    skill_content: str,
) -> None:
    """Resolving a skill agent should return the correct agent class and config."""
    mng_config = MngConfig()
    resolved = resolve_agent_type(AgentTypeName(type_name), mng_config)

    assert resolved.agent_class == agent_class
    assert isinstance(resolved.agent_config, config_class)
    assert resolved.agent_config.command == CommandString("claude")


# ── Skill content tests ─────────────────────────────────────────────────


@pytest.mark.parametrize("type_name,agent_class,config_class,skill_name,skill_content", _SKILL_AGENTS)
def test_skill_content_has_valid_frontmatter(
    type_name: str,
    agent_class: type,
    config_class: type,
    skill_name: str,
    skill_content: str,
) -> None:
    """The skill content should have valid YAML frontmatter with name and description."""
    assert skill_content.startswith("---\n")
    second_separator = skill_content.index("---", 4)
    assert second_separator > 0
    frontmatter = skill_content[4:second_separator]
    assert "name:" in frontmatter
    assert "description:" in frontmatter


@pytest.mark.parametrize("type_name,agent_class,config_class,skill_name,skill_content", _SKILL_AGENTS)
def test_skill_content_is_substantial(
    type_name: str,
    agent_class: type,
    config_class: type,
    skill_name: str,
    skill_content: str,
) -> None:
    """The skill content should be a meaningful set of instructions."""
    assert len(skill_content) > 100
    assert skill_name == type_name


@pytest.mark.parametrize("type_name,agent_class,config_class,skill_name,skill_content", _SKILL_AGENTS)
def test_skill_content_does_not_reference_skill_md(
    type_name: str,
    agent_class: type,
    config_class: type,
    skill_name: str,
    skill_content: str,
) -> None:
    """Skill content should not reference SKILL.md (it IS the skill file)."""
    assert "SKILL.md" not in skill_content


# ── Subclass tests ───────────────────────────────────────────────────────


@pytest.mark.parametrize("type_name,agent_class,config_class,skill_name,skill_content", _SKILL_AGENTS)
def test_skill_agent_is_subclass_of_claude_agent(
    type_name: str,
    agent_class: type,
    config_class: type,
    skill_name: str,
    skill_content: str,
) -> None:
    """Skill-provisioned agents should be subclasses of ClaudeAgent."""
    assert issubclass(agent_class, ClaudeAgent)
    assert issubclass(agent_class, SkillProvisionedAgent)


# ── Skill content-specific tests ─────────────────────────────────────────


def test_code_guardian_skill_content_contains_inconsistency_instructions() -> None:
    """The code-guardian skill should contain inconsistency-finding instructions."""
    assert "inconsistencies" in _CODE_GUARDIAN_SKILL_CONTENT.lower()
    assert "_tasks/inconsistencies/" in _CODE_GUARDIAN_SKILL_CONTENT


def test_fixme_fairy_skill_content_contains_fixme_instructions() -> None:
    """The fixme-fairy skill should contain FIXME-fixing instructions."""
    assert "fixme" in _FIXME_FAIRY_SKILL_CONTENT.lower()
    assert "uv run pytest" in _FIXME_FAIRY_SKILL_CONTENT


# ── Skill installation tests ────────────────────────────────────────────


@pytest.mark.parametrize("skill_name,skill_content", _SKILL_CONTENTS)
def test_install_skill_locally_creates_skill_file_in_non_interactive_mode(
    skill_name: str,
    skill_content: str,
    temp_mng_ctx: MngContext,
) -> None:
    """In non-interactive mode, _install_skill_locally should create the skill file without prompting."""
    skill_path = Path.home() / ".claude" / "skills" / skill_name / "SKILL.md"
    assert not skill_path.exists()

    _install_skill_locally(skill_name, skill_content, temp_mng_ctx)

    assert skill_path.exists()
    assert skill_path.read_text() == skill_content


@pytest.mark.parametrize("skill_name,skill_content", _SKILL_CONTENTS)
def test_install_skill_locally_overwrites_existing_skill_in_non_interactive_mode(
    skill_name: str,
    skill_content: str,
    temp_mng_ctx: MngContext,
) -> None:
    """In non-interactive mode, _install_skill_locally should overwrite an existing skill file."""
    skill_path = Path.home() / ".claude" / "skills" / skill_name / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text("old content")

    _install_skill_locally(skill_name, skill_content, temp_mng_ctx)

    assert skill_path.read_text() == skill_content


@pytest.mark.parametrize("skill_name,skill_content", _SKILL_CONTENTS)
def test_install_skill_locally_skips_when_content_unchanged(
    skill_name: str,
    skill_content: str,
    temp_mng_ctx: MngContext,
) -> None:
    """When skill content is already up to date, installation should be skipped."""
    skill_path = Path.home() / ".claude" / "skills" / skill_name / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(skill_content)
    original_mtime = skill_path.stat().st_mtime

    _install_skill_locally(skill_name, skill_content, temp_mng_ctx)

    # File should not have been rewritten (mtime unchanged)
    assert skill_path.stat().st_mtime == original_mtime


@pytest.mark.parametrize("skill_name,skill_content", _SKILL_CONTENTS)
def test_install_skill_locally_auto_approve_installs_without_prompting(
    skill_name: str,
    skill_content: str,
    temp_config: MngConfig,
    temp_profile_dir: Path,
    plugin_manager: "pluggy.PluginManager",
) -> None:
    """With is_auto_approve=True and is_interactive=True, skill should install without prompting."""
    with ConcurrencyGroup(name="test-auto-approve") as cg:
        auto_approve_ctx = make_mng_ctx(
            temp_config,
            plugin_manager,
            temp_profile_dir,
            is_interactive=True,
            is_auto_approve=True,
            concurrency_group=cg,
        )
        skill_path = Path.home() / ".claude" / "skills" / skill_name / "SKILL.md"
        assert not skill_path.exists()

        _install_skill_locally(skill_name, skill_content, auto_approve_ctx)

        assert skill_path.exists()
        assert skill_path.read_text() == skill_content
