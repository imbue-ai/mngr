"""Release test: full end-to-end flow for the antigravity (``agy``) agent type.

Drives the real ``mngr`` CLI against the real ``agy`` binary through the shared agent
release lifecycle (create -> WAITING -> message -> transcript -> stop/start resume ->
destroy). The arc and assertions live in ``imbue.mngr.agents.agent_release_testing``;
this file supplies antigravity's plumbing via an :class:`AgentReleaseProfile`.

antigravity authenticates from ``$HOME/.gemini`` (it has no config-dir override -- the
plugin relocates ``$HOME`` per agent and shares the oauth token by symlink). The autouse
``setup_test_mngr_env`` fixture redirects ``$HOME`` to a temp dir, so this test seeds the
real ``~/.gemini`` auth (including the shared oauth token, when agy wrote one rather than
keeping it in the macOS keychain) and a ``settings.json`` into that redirected home for the
plugin to find. On macOS it also seeds ``Library/Keychains`` (a symlink to the real one) so the
plugin's keychain symlink resolves -- without it agy's os_crypt blocks on a "keychain
cannot be found" dialog. Everything else agy needs -- HOME relocation, the non-dotted
workspace symlink, trust seeding, NUX skip -- the plugin handles itself.

The model is pinned to a Claude model via the seeded ``settings.json`` (agy reads its
model from settings, so the spaces/parens in the display name never reach a shell): agy's
default Gemini model can hit per-account usage limits, whereas a Claude model is reliable.

Requires the ``agy`` binary on PATH and a signed-in agy: ``~/.gemini/oauth_creds.json``, or
``MNGR_AGY_TEST_SIGNED_IN=1`` where agy keeps its sign-in in the OS keyring and writes no file (macOS);
skipped otherwise. Not run in CI.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

import psutil
import pytest

from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import AgentReleaseProfile
from imbue.mngr.agents.agent_release_testing import run_agent_release_lifecycle
from imbue.mngr.agents.agent_release_testing import run_message_delivery_journey
from imbue.mngr.agents.agent_release_testing import run_question_mark_message_delivery
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.hosts.common import is_macos
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import BackgroundTaskKind
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import mngr_agent_cleanup
from imbue.mngr.utils.testing import run_mngr_subprocess
from imbue.mngr_antigravity.antigravity_config import get_antigravity_oauth_token_path
from imbue.mngr_antigravity.antigravity_config import get_antigravity_settings_path
from imbue.mngr_antigravity.resources.testing import read_installed_agy_version

# Resolved at import time, before the autouse fixture redirects $HOME: the real ~/.gemini
# auth the plugin reads and shares into each per-agent home. agy signs in from the
# top-level oauth creds (``oauth_creds.json``/``google_accounts.json``), not just the
# antigravity-cli token -- whichever of them exist must be seeded into the redirected HOME
# or the agent comes up unauthenticated and can't run a turn.
_REAL_GEMINI = Path.home() / ".gemini"
_REAL_OAUTH_TOKEN = get_antigravity_oauth_token_path(Path.home())
_REAL_SETTINGS = get_antigravity_settings_path(Path.home())
_TOP_LEVEL_AUTH_FILES = ("oauth_creds.json", "google_accounts.json")

# Resolved at import time too: the user's real macOS login keychain dir. agy's embedded
# Chromium os_crypt resolves the keychain at $HOME/Library/Keychains, so under the
# redirected test HOME it finds none and raises a *blocking* "A keychain cannot be found
# to store Antigravity Safe Storage" dialog -- hanging the headless run. The plugin's
# _provision_macos_keychain symlinks the per-agent home's Library/Keychains to *host_home*'s
# (here the redirected HOME), so we seed that with a symlink to the real dir, exactly as the
# .gemini auth above is seeded from the real home. macOS-only; Linux has no such keychain.
_REAL_MACOS_KEYCHAINS = Path.home() / "Library" / "Keychains"

# An agy "models" display name for a Claude model. agy's default Gemini model can hit
# per-account usage limits; a Claude model is reliable. Seeded into settings.json, so the
# spaces/parens never reach a shell (unlike a model passed as a cli_arg). Overridable via
# MNGR_AGY_TEST_MODEL so a run can switch to a model that still has quota (agy quotas are
# per-model) -- must be an exact `agy models` display name. Update the default if the
# account's available models change.
_MODEL = os.environ.get("MNGR_AGY_TEST_MODEL", "Claude Sonnet 4.6 (Thinking)")

# Set to 1 to vouch that agy is signed in where it keeps its sign-in in the OS keyring and writes no file to check.
_SIGNED_IN_ENV_VAR: Final[str] = "MNGR_AGY_TEST_SIGNED_IN"

# Outlasts the turn that starts the command, which the test then ends itself.
_BACKGROUND_SLEEP_SECONDS: Final[int] = 317
_BACKGROUND_SLEEP_PROMPT: Final[str] = (
    f"Use your command tool to run exactly `sleep {_BACKGROUND_SLEEP_SECONDS}`, with nothing else on the command "
    "line (no `&`), and let the tool leave it running in the background: do not wait for it to finish and do not "
    "check on it. Then reply with just OK."
)
_CREATE_TIMEOUT_SECONDS: Final[float] = 600.0
_TURN_TIMEOUT_SECONDS: Final[float] = 300.0
_LIST_TIMEOUT_SECONDS: Final[float] = 120.0


class _AntigravityReleaseProfile(AgentReleaseProfile):
    agent_type = "antigravity"
    common_transcript_subdir = "antigravity"
    # agy observes the RUNNING marker (it sets the marker on PreInvocation and its Enter path
    # waits on the busy-signal, so the marker is present once `message` returns). It does not
    # report token usage, so that assertion is off.
    # FIXME(agy-forces-tool-call): forces_tool_call stays False because a single forced-tool
    # turn never carries a tool_result. agy DOES make the call (the converter surfaces the
    # nested assistant tool_call), but no tool_result lands, for two compounding reasons:
    # agy runs the command async (WaitMsBeforeAsync) and ends the turn before the result step
    # settles, and decode_agy_transcript.py never decodes CODE_ACTION/tool-output content (it
    # fills `content` only for USER_INPUT/PLANNER_RESPONSE/ERROR_MESSAGE), so the converter
    # drops it. Capture is a live ~1s SQLite poll plus a turn-end flush, not a turn snapshot.
    # Confirmed independent of quota (reproduced on a quota-healthy Gemini model) and prompt.
    # Enabling this needs the decoder to surface the real tool-output step type AND agy to
    # persist a settled result in-turn; see the investigation noted in the PR.
    forces_tool_call = False
    asserts_usage = False
    # This is the store the adopt-from-preserved arc adopts: after destroy, the harness
    # creates a fresh agent in a new worktree that adopts the just-preserved conversation
    # (via --adopt) and asserts it recalls the secret.
    native_session_preserved_relpaths = ("plugin/antigravity/home/.gemini/antigravity-cli/conversations",)

    def adopt_session_arg(self, preserved_dir: Path) -> str:
        # agy resumes by conversation id (directory-agnostic), so hand the adopting create the
        # root conversation id -- the plugin resolves it against the preserved store and copies
        # it into the new agent's home. The preserved dir mirrors the agent state dir, so the
        # root_conversation pointer file sits at its top level.
        return (preserved_dir / "root_conversation").read_text().strip()

    def unavailable_reason(self) -> str | None:
        is_signed_in = (_REAL_GEMINI / "oauth_creds.json").exists() or os.environ.get(_SIGNED_IN_ENV_VAR) == "1"
        if shutil.which("agy") is None or not is_signed_in:
            return (
                "Release test requires the `agy` binary on PATH and a signed-in agy: ~/.gemini/oauth_creds.json, or "
                f"{_SIGNED_IN_ENV_VAR}=1 where agy keeps its sign-in in the OS keyring."
            )
        return None

    def setup(self, tmp_path: Path) -> AgentReleaseContext:
        env = get_subprocess_test_env(root_name="mngr-antigravity-release-test")
        project_config_dir = tmp_path / ".mngr-antigravity-test"
        project_config_dir.mkdir(parents=True, exist_ok=True)
        (project_config_dir / "settings.local.toml").write_text(
            "is_allowed_in_pytest = true\n\n[providers.modal]\nis_enabled = false\n\n[providers.docker]\nis_enabled = false\n"
        )
        env["MNGR_PROJECT_CONFIG_DIR"] = str(project_config_dir)

        # Seed the real ~/.gemini auth + a settings.json (pinning a Claude model) into the
        # autouse-redirected $HOME, where the plugin reads them to authenticate and share:
        # the top-level oauth creds (so agy is actually signed in), the antigravity-cli
        # token when agy wrote one, and settings.
        seeded_token = get_antigravity_oauth_token_path(Path(env["HOME"]))
        seeded_token.parent.mkdir(parents=True, exist_ok=True)
        seeded_gemini = Path(env["HOME"]) / ".gemini"
        for name in _TOP_LEVEL_AUTH_FILES:
            source = _REAL_GEMINI / name
            if source.exists():
                shutil.copy2(source, seeded_gemini / name)
        # agy stores its token in the OS keyring when one exists, so on macOS the file may be
        # absent and the keychain seeded below is what signs the agent in.
        if _REAL_OAUTH_TOKEN.exists():
            shutil.copy2(_REAL_OAUTH_TOKEN, seeded_token)
        settings = json.loads(_REAL_SETTINGS.read_text()) if _REAL_SETTINGS.exists() else {}
        settings["model"] = _MODEL
        get_antigravity_settings_path(Path(env["HOME"])).write_text(json.dumps(settings))

        # macOS only: seed Library/Keychains into the redirected HOME so the plugin's
        # _provision_macos_keychain (which symlinks host_home's Library/Keychains into the
        # per-agent home) resolves to the user's real login keychain. Without this, agy's
        # os_crypt finds no keychain and blocks on a modal dialog (see _REAL_MACOS_KEYCHAINS).
        if is_macos() and _REAL_MACOS_KEYCHAINS.exists():
            seeded_keychains = Path(env["HOME"]) / "Library" / "Keychains"
            seeded_keychains.parent.mkdir(parents=True, exist_ok=True)
            seeded_keychains.symlink_to(_REAL_MACOS_KEYCHAINS)

        work_dir = tmp_path / "work"
        init_git_repo(work_dir, initial_commit=True)
        return AgentReleaseContext(env=env, project_dir=work_dir, host_dir=Path(env["MNGR_HOST_DIR"]))

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        return ["--no-ensure-clean", "--source", str(ctx.project_dir)]

    def run_mngr(self, ctx: AgentReleaseContext, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return run_mngr_subprocess(*args, env=dict(ctx.env), timeout=timeout)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
# Known-flaky: the post-resume "recall" step occasionally hits agy's TUI "Timeout waiting for
# message submission signal (waited 90.0s)" and fails -- the conversation restores correctly,
# only the message submit into the resumed TUI hangs. Seen once in two local runs on agy 1.0.8.
@pytest.mark.flaky
# Outer wall-clock safety net around the harness's per-phase poll timeouts -- not a measure of
# expected runtime. A healthy run is ~25s (measured: one local run passed in 24s; a flaky run
# that hung 90s on the recall submit still finished in ~111s). Lowered from 1500s, which was
# copied from the opencode/pi sibling tests before this test had ever completed a run.
@pytest.mark.timeout(600)
def test_antigravity_agent_full_lifecycle(tmp_path: Path) -> None:
    """The antigravity agent survives the full real-CLI release lifecycle end to end.

    Drives ``mngr`` against the real ``agy`` binary through every stage of the shared agent
    release arc and asserts each stage's effect: create reaches WAITING; a ``message`` turn
    completes and sets the RUNNING marker; the transcript surfaces the assistant's reply; a
    stop/start cycle resumes the same conversation so the agent still recalls a secret given
    before the restart; and an ``--adopt`` create in a fresh worktree, fed the preserved
    conversation id, recalls that same secret -- proving the native session was preserved and
    re-adopted. Each assertion fails if its stage were a no-op (auth not seeded, turn not run,
    transcript not decoded, resume/adopt not carrying conversation state forward).
    """
    run_agent_release_lifecycle(_AntigravityReleaseProfile(), tmp_path)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.flaky
@pytest.mark.timeout(900)
def test_antigravity_message_delivery_journey(tmp_path: Path) -> None:
    """Drive the evidence-confirmed send pipeline through its racey delivery scenarios.

    agy confirms sends via its ``active`` marker (maintained by the statusLine
    on every busy sample), so this proves the marker-probe path: idle delivery,
    send-while-busy, rapid sequential sends, and a long buffer-pasted message
    -- each delivered exactly once. Marked flaky like the lifecycle test: agy
    shares its infrastructure-level startup variance.
    """
    run_message_delivery_journey(_AntigravityReleaseProfile(), tmp_path)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.flaky
@pytest.mark.timeout(900)
def test_antigravity_question_mark_messages_are_delivered_whole(tmp_path: Path) -> None:
    """A lone ``?`` and a message starting with ``?`` each land as one whole turn.

    Typed into an empty input box, agy takes a ``?`` as its help key and the help screen
    swallows the keys after it, so these messages are pasted.
    """
    run_question_mark_message_delivery(_AntigravityReleaseProfile(), tmp_path)


def _read_listed_agent(profile: AgentReleaseProfile, ctx: AgentReleaseContext, agent_name: str) -> dict[str, Any]:
    """The agent as ``mngr list`` reports it."""
    listing = profile.run_mngr(ctx, "list", "--provider", "local", "--format", "json", timeout=_LIST_TIMEOUT_SECONDS)
    assert listing.returncode == 0, f"mngr list failed:\n{listing.stdout}\n{listing.stderr}"
    (listed_agent,) = [agent for agent in json.loads(listing.stdout)["agents"] if agent["name"] == agent_name]
    return listed_agent


def _read_state_and_background_tasks(
    profile: AgentReleaseProfile, ctx: AgentReleaseContext, agent_name: str
) -> tuple[str, list[tuple[str, str]]]:
    """The agent's state and the kind and native type of each of its background tasks, as ``mngr list`` reports them."""
    listed_agent = _read_listed_agent(profile, ctx, agent_name)
    return listed_agent["state"], [(task["kind"], task["native_type"]) for task in listed_agent["background_tasks"]]


@pytest.mark.release
@pytest.mark.harness_internals(harness="antigravity")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_antigravity_agent_reads_running_background_while_a_backgrounded_command_runs(tmp_path: Path) -> None:
    """A turn that leaves a command running reads RUNNING_BACKGROUND with one SHELL task, then WAITING once it ends.

    Pins that agy's statusLine payload counts the command in ``task_count`` and stops counting it when it ends.
    """
    profile = _AntigravityReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    agent_name = f"antigravity-background-{get_short_random_string()}"
    drift = (
        f"(agy version: {read_installed_agy_version()}; consumer: statusline.sh, which writes one agy-task-<n> "
        "record per task the statusLine payload's task_count counts)"
    )
    with mngr_agent_cleanup(agent_name, env=dict(ctx.env)):
        create = profile.run_mngr(
            ctx,
            "create",
            agent_name,
            profile.agent_type,
            "--no-connect",
            "--yes",
            *profile.create_extra_args(ctx),
            "-S",
            "agent_types.antigravity.auto_allow_permissions=true",
            timeout=_CREATE_TIMEOUT_SECONDS,
        )
        assert create.returncode == 0, f"create failed:\n{create.stdout}\n{create.stderr}"

        send_expecting_success(profile, ctx, agent_name, _BACKGROUND_SLEEP_PROMPT)
        assert poll_until(
            lambda: _read_state_and_background_tasks(profile, ctx, agent_name)
            == (AgentLifecycleState.RUNNING_BACKGROUND, [(BackgroundTaskKind.SHELL, "task_count")]),
            timeout=_TURN_TIMEOUT_SECONDS,
            poll_interval=2.0,
        ), (
            f"after the turn that backgrounded `sleep {_BACKGROUND_SLEEP_SECONDS}` the agent never read "
            "RUNNING_BACKGROUND with one SHELL task of native type task_count; mngr list reports "
            f"{_read_state_and_background_tasks(profile, ctx, agent_name)} {drift}"
        )

        agy_pid = _read_listed_agent(profile, ctx, agent_name)["pid"]
        sleep_processes = [
            process
            for process in psutil.Process(agy_pid).children(recursive=True)
            if process.as_dict(["cmdline"])["cmdline"] == ["sleep", str(_BACKGROUND_SLEEP_SECONDS)]
        ]
        assert len(sleep_processes) == 1, (
            f"expected one `sleep {_BACKGROUND_SLEEP_SECONDS}` under the agent's process {agy_pid}, "
            f"found {sleep_processes}"
        )
        sleep_processes[0].terminate()
        assert poll_until(
            lambda: _read_state_and_background_tasks(profile, ctx, agent_name) == (AgentLifecycleState.WAITING, []),
            timeout=_TURN_TIMEOUT_SECONDS,
            poll_interval=2.0,
        ), (
            "the agent never read WAITING with no background task after its backgrounded command ended; mngr list "
            f"reports {_read_state_and_background_tasks(profile, ctx, agent_name)} {drift}"
        )
