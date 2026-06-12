"""Tests for skill-provisioned agent types (code-guardian, fixme-fairy)."""

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from pydantic import Field

from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.config.agent_class_registry import get_agent_class
from imbue.mngr.config.agent_config_registry import get_agent_config_class
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr_claude.code_guardian_agent import CodeGuardianAgent
from imbue.mngr_claude.code_guardian_agent import CodeGuardianAgentConfig
from imbue.mngr_claude.code_guardian_agent import _CODE_GUARDIAN_SKILL_CONTENT
from imbue.mngr_claude.code_guardian_agent import _SKILL_NAME as CODE_GUARDIAN_SKILL_NAME
from imbue.mngr_claude.fixme_fairy_agent import FixmeFairyAgent
from imbue.mngr_claude.fixme_fairy_agent import FixmeFairyAgentConfig
from imbue.mngr_claude.fixme_fairy_agent import _FIXME_FAIRY_SKILL_CONTENT
from imbue.mngr_claude.fixme_fairy_agent import _SKILL_NAME as FIXME_FAIRY_SKILL_NAME
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.mngr_claude.skill_agent import SkillProvisionedAgent
from imbue.mngr_claude.skill_agent import SkillProvisionedAgentConfig
from imbue.mngr_claude.skill_agent import _install_skill_locally
from imbue.mngr_claude.skill_agent import _install_skill_remotely

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
    mngr_config = MngrConfig()
    resolved = resolve_agent_type(AgentTypeName(type_name), mngr_config)

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
def test_skill_content_has_instructional_body(
    type_name: str,
    agent_class: type,
    config_class: type,
    skill_name: str,
    skill_content: str,
) -> None:
    """The skill content should have an instructional body beyond its frontmatter."""
    second_separator = skill_content.index("---", 4)
    body = skill_content[second_separator + 3 :]
    # A real skill body has at least one markdown section heading of instructions.
    assert "## " in body


@pytest.mark.parametrize("type_name,agent_class,config_class,skill_name,skill_content", _SKILL_AGENTS)
def test_skill_name_matches_registered_type_name(
    type_name: str,
    agent_class: type,
    config_class: type,
    skill_name: str,
    skill_content: str,
) -> None:
    """The installed skill name should match the registered agent type name."""
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
def test_install_skill_locally_creates_skill_file_in_per_agent_config_dir(
    skill_name: str,
    skill_content: str,
    tmp_path: Path,
) -> None:
    """_install_skill_locally writes the skill into the agent's own config dir, not global ~/.claude."""
    skill_path = tmp_path / "skills" / skill_name / "SKILL.md"
    assert not skill_path.exists()

    _install_skill_locally(skill_name, skill_content, tmp_path)

    assert skill_path.exists()
    assert skill_path.read_text() == skill_content


@pytest.mark.parametrize("skill_name,skill_content", _SKILL_CONTENTS)
def test_install_skill_locally_overwrites_existing_skill(
    skill_name: str,
    skill_content: str,
    tmp_path: Path,
) -> None:
    """_install_skill_locally should overwrite an existing skill file holding stale content."""
    skill_path = tmp_path / "skills" / skill_name / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text("old content")

    _install_skill_locally(skill_name, skill_content, tmp_path)

    assert skill_path.read_text() == skill_content


@pytest.mark.parametrize("skill_name,skill_content", _SKILL_CONTENTS)
def test_install_skill_locally_skips_when_content_unchanged(
    skill_name: str,
    skill_content: str,
    tmp_path: Path,
) -> None:
    """When skill content is already up to date, installation should leave the file untouched."""
    skill_path = tmp_path / "skills" / skill_name / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(skill_content)

    _install_skill_locally(skill_name, skill_content, tmp_path)

    # The file content must be exactly the already-installed content (not rewritten).
    assert skill_path.read_text() == skill_content


def test_install_skill_locally_breaks_symlink_into_shared_skills(tmp_path: Path) -> None:
    """When the per-agent skills/<name> is a child-symlink into the shared ~/.claude/skills/
    (as _sync_user_resources leaves it), install must break the symlink and write a real
    file, rather than following it and corrupting the shared source."""
    shared_skill_dir = tmp_path / "home_claude" / "skills" / "code-guardian"
    shared_skill_dir.mkdir(parents=True)
    shared_skill_file = shared_skill_dir / "SKILL.md"
    shared_skill_file.write_text("shared content")

    config_dir = tmp_path / "agent_config"
    agent_skills_dir = config_dir / "skills"
    agent_skills_dir.mkdir(parents=True)
    # Child-level symlink, mirroring what _sync_user_resources creates.
    (agent_skills_dir / "code-guardian").symlink_to(shared_skill_dir)

    _install_skill_locally("code-guardian", "agent content", config_dir)

    # The agent's skill is now a real file holding the agent content...
    assert not (agent_skills_dir / "code-guardian").is_symlink()
    assert (agent_skills_dir / "code-guardian" / "SKILL.md").read_text() == "agent content"
    # ...and the shared source is untouched.
    assert shared_skill_file.read_text() == "shared content"


# -- SkillProvisionedAgentConfig tests --


def test_skill_provisioned_agent_config_can_be_instantiated() -> None:
    """SkillProvisionedAgentConfig can be instantiated with default values."""
    config = SkillProvisionedAgentConfig()
    assert config.command == CommandString("claude")
    assert isinstance(config, ClaudeAgentConfig)


def test_skill_provisioned_agent_config_accepts_custom_command() -> None:
    """SkillProvisionedAgentConfig should accept a custom command override."""
    config = SkillProvisionedAgentConfig(command=CommandString("custom-agent"))
    assert config.command == CommandString("custom-agent")


# -- Remote skill installation tests --


class _RecordingFakeHost(FakeHost):
    """FakeHost that records skill writes and idempotent commands without touching disk.

    _install_skill_remotely writes to a relative ``.claude/skills/...`` path; recording
    the calls (instead of letting FakeHost write to the process cwd) keeps the test
    hermetic while still asserting the exact path and content that would be written.
    """

    written_files: list[tuple[Path, str]] = Field(default_factory=list, description="(path, content) writes")
    idempotent_commands: list[str] = Field(default_factory=list, description="Recorded idempotent commands")

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.idempotent_commands.append(command)
        return CommandResult(stdout="", stderr="", success=True)

    def write_text_file(self, path: Path, content: str, encoding: str = "utf-8", mode: str | None = None) -> None:
        self.written_files.append((path, content))


@pytest.mark.parametrize("skill_name,skill_content", _SKILL_CONTENTS)
def test_install_skill_remotely_writes_skill_file_and_creates_dir(
    skill_name: str,
    skill_content: str,
) -> None:
    """_install_skill_remotely should write the skill to the host's .claude/skills path and mkdir it."""
    host = _RecordingFakeHost()

    _install_skill_remotely(skill_name, skill_content, cast(OnlineHostInterface, host))

    assert host.written_files == [(Path(f".claude/skills/{skill_name}/SKILL.md"), skill_content)]
    assert any(f"mkdir -p ~/.claude/skills/{skill_name}" == command for command in host.idempotent_commands)
