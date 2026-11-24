import os
import shutil
import subprocess
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import AgentReleaseProfile
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr.utils.testing import run_mngr_subprocess
from imbue.mngr_pi_coding.plugin import PiCodingAgent
from imbue.mngr_pi_coding.plugin import PiCodingAgentConfig

# A fast, cheap, tool-capable model keeps the real turns short.
RELEASE_TEST_MODEL: Final[str] = "claude-haiku-4-5"


def make_local_pi_agent(
    local_provider: LocalProviderInstance, tmp_path: Path, agent_config: PiCodingAgentConfig
) -> PiCodingAgent:
    """A PiCodingAgent on the local provider's real host, working in ``tmp_path/work``."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    return PiCodingAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-pi"),
        agent_type=AgentTypeName("pi-coding"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=agent_config,
        host=host,
    )


def _commit_project_trust_inputs(work_dir: Path) -> None:
    """Make ``work_dir`` a git repo whose ``.agents/skills`` dir gives pi 0.79+ "project trust inputs"."""
    init_git_repo(work_dir, initial_commit=True)
    (work_dir / ".gitignore").write_text(".pi/\n")
    skills_dir = work_dir / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "example.md").write_text("# Example skill (gives the worktree pi trust inputs)\n")
    run_git_command(work_dir, "add", ".gitignore", ".agents")
    run_git_command(work_dir, "commit", "-m", "add gitignore and .agents/skills")


class PiReleaseProfile(AgentReleaseProfile):
    """A real pi on an Anthropic API key, against a source repo with pi trust inputs that `mngr create --yes` trusts."""

    agent_type = "pi-coding"
    common_transcript_subdir = "pi-coding"
    forces_tool_call = True
    asserts_usage = True
    native_session_preserved_relpaths = ("plugin/pi_coding/sessions",)

    def unavailable_reason(self) -> str | None:
        if shutil.which("pi") is None or not os.environ.get("ANTHROPIC_API_KEY"):
            return "Release test requires ANTHROPIC_API_KEY in the environment and `pi` on PATH."
        return None

    def setup(self, tmp_path: Path) -> AgentReleaseContext:
        env = get_subprocess_test_env(root_name="mngr-pi-release-test")
        project_config_dir = tmp_path / ".mngr-pi-test"
        project_config_dir.mkdir(parents=True, exist_ok=True)
        (project_config_dir / "settings.local.toml").write_text(
            "is_allowed_in_pytest = true\n\n[providers.modal]\nis_enabled = false\n\n[providers.docker]\nis_enabled = false\n"
        )
        env["MNGR_PROJECT_CONFIG_DIR"] = str(project_config_dir)
        work_dir = tmp_path / "pi-source"
        _commit_project_trust_inputs(work_dir)
        return AgentReleaseContext(env=env, project_dir=work_dir, host_dir=Path(env["MNGR_HOST_DIR"]))

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        return [
            "--no-ensure-clean",
            "--source",
            str(ctx.project_dir),
            "--pass-env",
            "ANTHROPIC_API_KEY",
            "--",
            "--provider",
            "anthropic",
            "--model",
            RELEASE_TEST_MODEL,
        ]

    def adopt_session_arg(self, preserved_dir: Path) -> str:
        """Return the absolute path to the preserved pi session JSONL to adopt.

        ``pi_session_file`` (preserved alongside the store) holds the live agent's
        session path; only its basename is stable across the destroy, so locate the
        matching JSONL under the preserved ``sessions`` store and return that path.
        """
        pointer = preserved_dir / "pi_session_file"
        session_basename = Path(pointer.read_text().strip()).name
        sessions_root = preserved_dir / "plugin" / "pi_coding" / "sessions"
        matches = [path for path in sessions_root.glob(f"*/{session_basename}")]
        assert len(matches) == 1, (
            f"expected exactly one preserved pi session named {session_basename} under {sessions_root}, "
            f"found {matches}"
        )
        return str(matches[0])

    def prepare_adoption_project_dir(self, work_dir: Path) -> None:
        _commit_project_trust_inputs(work_dir)

    def run_mngr(self, ctx: AgentReleaseContext, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return run_mngr_subprocess(*args, env=dict(ctx.env), timeout=timeout)
