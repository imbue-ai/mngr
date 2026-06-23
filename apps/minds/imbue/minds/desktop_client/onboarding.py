"""Onboarding side effects for the workspace-creation flow.

While a workspace is being created in the background, the desktop client
asks the user three short questions. This module applies the answers:

1. ``data_preference`` -- unless the user chose ``CONTROL``, run a minimal
   local scan of the user's machine (currently just their name) and write
   it to a per-creation JSON file. Nothing consumes this file yet; it is
   the seed of a feature we will extend later.
2. ``initial_problem`` -- once the workspace's chat agent is online, send
   the text to it as a follow-up ``mngr message`` (the baked-in
   ``/welcome`` message is left intact).
3. ``permissions_preference`` -- once the workspace is ready, write the
   text into the workspace's Claude memory at
   ``runtime/memory/permissions_preferences.md`` via ``mngr exec``.

Each answer is optional: an empty / ``CONTROL`` answer is a no-op. The work
runs on a detached background thread so the answer-submission request
returns immediately and the user is never blocked on it.
"""

import base64
import getpass
import json
import os
import pwd
import threading
import time
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreationStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import UserDataPreference
from imbue.mngr.primitives import AgentId

# Directory under the minds data root where per-creation user-context JSON
# files are written by the Q1 scan.
USER_CONTEXT_DIR_NAME: Final[str] = "user_context"

# Placeholder ``details`` value written by the (currently minimal) Q1 scan.
USER_CONTEXT_PLACEHOLDER_DETAILS: Final[str] = "couldn't find any details"

# Absolute path inside the workspace container where the Q3 permissions
# preference is written. Lives in the agent's Claude memory directory
# (``runtime/memory/``); ``/mngr/code`` is the container WORKDIR.
PERMISSIONS_PREFERENCES_REMOTE_PATH: Final[str] = "/mngr/code/runtime/memory/permissions_preferences.md"

# Expected wall-clock duration of ``mngr create`` per compute provider,
# used only to drive the client-side progress-bar animation on the
# creating page (the bar eases toward ~80% over this duration). These are
# rough estimates, not guarantees.
# LIMA now boots a VM *and* builds the project image inside it (the agent runs
# in a Docker container in the VM), so a cold create is closer to a VPS build
# than the old run-directly-in-the-VM path -- bump its progress-bar estimate
# accordingly.
EXPECTED_CREATION_DURATION_SECONDS_BY_LAUNCH_MODE: Final[dict[LaunchMode, float]] = {
    LaunchMode.DOCKER: 30.0,
    LaunchMode.LIMA: 600.0,
    LaunchMode.VULTR: 300.0,
    LaunchMode.AWS: 300.0,
    LaunchMode.IMBUE_CLOUD: 30.0,
}

# Fallback when the launch mode is somehow not in the map above.
DEFAULT_EXPECTED_CREATION_DURATION_SECONDS: Final[float] = 60.0

_MNGR_EXEC_TIMEOUT_SECONDS: Final[float] = 30.0
_GIT_CONFIG_TIMEOUT_SECONDS: Final[float] = 10.0


class OnboardingAnswers(FrozenModel):
    """The three onboarding answers collected while a workspace is created.

    Every field is optional: ``data_preference`` is ``None`` when the
    question was skipped, and the two text fields are empty strings when
    skipped or left blank. Each maps to a no-op when absent.
    """

    data_preference: UserDataPreference | None = Field(
        default=None,
        description="Q1: how much the agent may learn about the user; ``None`` if unanswered.",
    )
    initial_problem: str = Field(
        default="",
        description="Q2: the problem the user wants to start with; sent to the chat agent. Empty = skip.",
    )
    permissions_preference: str = Field(
        default="",
        description="Q3: the user's permissions preference; written to workspace memory. Empty = skip.",
    )

    @property
    def is_scan_requested(self) -> bool:
        """True when Q1 asks for the local user-context scan (any preference except ``CONTROL``)."""
        return self.data_preference is not None and self.data_preference is not UserDataPreference.CONTROL

    @property
    def is_noop(self) -> bool:
        """True when no answer triggers any side effect."""
        return (
            not self.is_scan_requested and not self.initial_problem.strip() and not self.permissions_preference.strip()
        )


@pure
def expected_creation_duration_seconds(launch_mode: LaunchMode) -> float:
    """Resolve the per-provider expected creation duration for the progress bar."""
    return EXPECTED_CREATION_DURATION_SECONDS_BY_LAUNCH_MODE.get(
        launch_mode, DEFAULT_EXPECTED_CREATION_DURATION_SECONDS
    )


@pure
def build_user_context_document(user_name: str) -> dict[str, str]:
    """Build the minimal user-context document written by the Q1 scan."""
    return {"name": user_name, "details": USER_CONTEXT_PLACEHOLDER_DETAILS}


@pure
def build_permissions_write_script(permissions_text: str) -> str:
    """Build the remote bash script that writes the Q3 preference into workspace memory.

    The text is base64-encoded so arbitrary content (quotes, newlines)
    survives transport through ``mngr exec``'s single command-string
    argument. The script creates the memory directory and overwrites the
    file.
    """
    encoded = base64.b64encode(permissions_text.encode("utf-8")).decode("ascii")
    directory = PERMISSIONS_PREFERENCES_REMOTE_PATH.rsplit("/", 1)[0]
    return f"set -e; mkdir -p {directory}; printf %s '{encoded}' | base64 -d > {PERMISSIONS_PREFERENCES_REMOTE_PATH}"


def resolve_local_user_name() -> str:
    """Resolve the user's name from the local machine.

    Tries, in order: git's configured ``user.name``, the OS account's full
    name (the first GECOS field), then the login username. The login
    username always exists, so this returns a non-empty string.
    """
    git_name = _read_git_user_name()
    if git_name:
        return git_name
    gecos_name = _read_os_full_name()
    if gecos_name:
        return gecos_name
    return getpass.getuser()


def _read_git_user_name() -> str:
    """Read ``git config --global user.name``; empty string if unset / unavailable."""
    cg = ConcurrencyGroup(name="onboarding-git-config")
    with cg:
        result = cg.run_process_to_completion(
            command=["git", "config", "--global", "user.name"],
            timeout=_GIT_CONFIG_TIMEOUT_SECONDS,
            is_checked_after=False,
        )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _read_os_full_name() -> str:
    """Read the current user's full name from the GECOS field; empty if unset."""
    try:
        entry = pwd.getpwuid(os.getuid())
    except KeyError:
        return ""
    return entry.pw_gecos.split(",", 1)[0].strip()


class OnboardingApplier(MutableModel):
    """Applies onboarding answers to an in-flight / freshly-created workspace.

    Construct one per desktop-client process and call ``start_apply`` once
    per workspace creation. All work happens on a detached thread tracked
    by ``root_concurrency_group``.
    """

    agent_creator: AgentCreator = Field(
        frozen=True,
        description="Polled to learn the workspace's host name and canonical agent id.",
    )
    paths: WorkspacePaths = Field(frozen=True, description="Minds data paths; the Q1 scan writes under data_dir.")
    message_sender: MngrMessageSender = Field(
        frozen=True,
        description="Sends the Q2 initial-problem message to the chat agent.",
    )
    root_concurrency_group: ConcurrencyGroup = Field(
        frozen=True,
        description="Root group the detached apply thread is tracked under.",
    )
    mngr_binary: str = Field(
        default=MNGR_BINARY, frozen=True, description="Path to the mngr binary for ``mngr exec``."
    )
    canonical_agent_id_wait_timeout_seconds: float = Field(
        default=600.0,
        frozen=True,
        description="Max time to wait for ``mngr create`` to publish the canonical agent id before giving up on Q3.",
    )
    chat_agent_message_timeout_seconds: float = Field(
        default=3600.0,
        frozen=True,
        description=(
            "Max time to keep retrying the Q2 message until the bootstrap-created chat agent accepts it. "
            "Set generously (1 hour) because the chat agent can take a long time to come online: a cold "
            "lima create boots a VM and builds the in-VM image first, and the user may still need to finish "
            "logging in to their AI provider before the agent will accept the message. This is not provider-"
            "specific -- it just has to outlast the slowest realistic 'workspace fully ready to chat' path."
        ),
    )
    poll_interval_seconds: float = Field(
        default=2.0,
        frozen=True,
        description="Sleep between polls while waiting for the canonical id / chat agent.",
    )

    def start_apply(self, creation_id: CreationId, answers: OnboardingAnswers) -> None:
        """Apply ``answers`` on a detached background thread; returns immediately.

        A no-op set of answers spawns no thread.
        """
        if answers.is_noop:
            logger.debug("Onboarding answers for creation {} are a no-op; nothing to apply", creation_id)
            return
        self.root_concurrency_group.start_new_thread(
            target=self._apply,
            kwargs={"creation_id": creation_id, "answers": answers},
            name=f"onboarding-apply-{creation_id}",
            # is_checked=False so a failure in an optional onboarding side
            # effect never poisons the root group; failures are logged.
            is_checked=False,
        )

    def _apply(self, creation_id: CreationId, answers: OnboardingAnswers) -> None:
        """Apply the answers; best-effort and isolated per side effect."""
        with log_span("Applying onboarding answers for creation {}", creation_id):
            # Q1: local scan. Independent of the workspace, so run it first.
            if answers.is_scan_requested:
                self._run_user_context_scan(creation_id)

            # Q2 (send the initial problem to the chat agent) and Q3 (write the
            # permissions preference into the workspace) are independent and
            # each blocks on its own condition -- Q2 waits for the bootstrap
            # chat agent to come online, Q3 waits for the canonical agent id --
            # so run them concurrently rather than letting a slow one hold up
            # the other.
            self._apply_workspace_answers(creation_id, answers)

    def _apply_workspace_answers(self, creation_id: CreationId, answers: OnboardingAnswers) -> None:
        """Apply Q2 and Q3 concurrently, waiting for both to finish."""
        is_problem_pending = bool(answers.initial_problem.strip())
        is_permissions_pending = bool(answers.permissions_preference.strip())
        if not is_problem_pending and not is_permissions_pending:
            return

        info = self.agent_creator.get_creation_info(creation_id)
        host_name = info.host_name if info is not None else ""

        # The child group's exit (the ``with`` block below) waits for both
        # strands to finish; size its exit timeout to cover their own
        # deadlines so a long-but-legitimate wait isn't cut short. The
        # shutdown timeout stays at the default so desktop-client shutdown is
        # never blocked on onboarding delivery.
        exit_timeout_seconds = (
            max(self.chat_agent_message_timeout_seconds, self.canonical_agent_id_wait_timeout_seconds)
            + self.poll_interval_seconds
        )
        delivery_group = self.root_concurrency_group.make_concurrency_group(
            name=f"onboarding-deliver-{creation_id}",
            exit_timeout_seconds=exit_timeout_seconds,
        )
        with delivery_group:
            # Each strand is ``is_checked=False`` and swallows / logs its own
            # failures, so an optional onboarding side effect can never poison
            # the group or the other strand.
            if is_problem_pending:
                if host_name:
                    delivery_group.start_new_thread(
                        target=self._send_initial_problem,
                        args=(host_name, answers.initial_problem),
                        name=f"onboarding-initial-problem-{creation_id}",
                        is_checked=False,
                    )
                else:
                    logger.error("Cannot send initial problem for creation {}: unknown host name", creation_id)
            if is_permissions_pending:
                delivery_group.start_new_thread(
                    target=self._apply_permissions_preference,
                    args=(creation_id, answers.permissions_preference),
                    name=f"onboarding-permissions-{creation_id}",
                    is_checked=False,
                )

    def _apply_permissions_preference(self, creation_id: CreationId, permissions_text: str) -> None:
        """Wait for the canonical agent id, then write the Q3 permissions preference."""
        agent_id = self._wait_for_canonical_agent_id(creation_id)
        if agent_id is not None:
            self._write_permissions_preference(agent_id, permissions_text)
        else:
            logger.error(
                "Gave up writing permissions preference for creation {}: no canonical agent id",
                creation_id,
            )

    def _run_user_context_scan(self, creation_id: CreationId) -> None:
        """Resolve the user's name and write the per-creation user-context JSON file."""
        user_name = resolve_local_user_name()
        document = build_user_context_document(user_name)
        context_dir = self.paths.data_dir / USER_CONTEXT_DIR_NAME
        context_dir.mkdir(parents=True, exist_ok=True)
        context_path = context_dir / f"{creation_id}.json"
        context_path.write_text(json.dumps(document, indent=2))
        logger.debug("Wrote user-context file for creation {} to {}", creation_id, context_path)

    def _wait_for_canonical_agent_id(self, creation_id: CreationId) -> AgentId | None:
        """Poll creation status until the canonical agent id is known, or give up.

        Returns the canonical id once ``mngr create`` publishes it, or
        ``None`` if the creation failed or the wait timed out.
        """
        deadline = time.monotonic() + self.canonical_agent_id_wait_timeout_seconds
        while time.monotonic() < deadline:
            info = self.agent_creator.get_creation_info(creation_id)
            if info is not None:
                if info.agent_id is not None:
                    return info.agent_id
                if info.status is AgentCreationStatus.FAILED:
                    return None
            threading.Event().wait(timeout=self.poll_interval_seconds)
        return None

    def _write_permissions_preference(self, agent_id: AgentId, permissions_text: str) -> None:
        """Write the Q3 permissions preference into the workspace's Claude memory via ``mngr exec``."""
        script = build_permissions_write_script(permissions_text)
        cg = ConcurrencyGroup(name="onboarding-mngr-exec")
        with cg:
            result = cg.run_process_to_completion(
                command=[self.mngr_binary, "exec", str(agent_id), script],
                timeout=_MNGR_EXEC_TIMEOUT_SECONDS,
                is_checked_after=False,
            )
        if result.returncode != 0:
            logger.error(
                "Failed to write permissions preference to agent {} (exit {}): {}",
                agent_id,
                result.returncode,
                result.stderr.strip(),
            )
        else:
            logger.debug("Wrote permissions preference to agent {}", agent_id)

    def _send_initial_problem(self, host_name: str, initial_problem: str) -> None:
        """Send the Q2 message to the chat agent, retrying until it exists or the timeout elapses.

        The chat agent is created asynchronously by the workspace's
        bootstrap and is named after the host, so it does not exist for the
        first several attempts. ``MngrMessageSender.deliver`` confirms the
        target actually received the message (via the structured ``mngr
        message`` output), so we keep retrying until a real delivery happens
        rather than stopping on the first non-error exit -- ``mngr message``
        exits 0 even when no agent matches, which would otherwise make us
        "succeed" without ever reaching the chat agent.
        """
        deadline = time.monotonic() + self.chat_agent_message_timeout_seconds
        while time.monotonic() < deadline:
            if self.message_sender.deliver(host_name, initial_problem):
                logger.debug("Delivered initial problem to chat agent {}", host_name)
                return
            threading.Event().wait(timeout=self.poll_interval_seconds)
        logger.error("Gave up delivering initial problem to chat agent {} after timeout", host_name)
