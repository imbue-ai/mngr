"""Release test: a real Docker workspace create drives an honest progress bar, and measures the seeds.

Creates one local Docker workspace from the default workspace template through
the desktop's own create path (``AgentCreator``), sampling the attempt's
progress the way the creation page polls it, and checks what only a real create
can: the bar moves through the desktop's steps, mngr's phases and the Docker
build's stages in order, never backwards, and ends at 100; and the durations the
create recorded land in the device history that later creates learn from.

The history is then exported to ``.test_output/create_progress/events.jsonl``,
which is what the weekly ``create-progress-defaults-sync`` workflow feeds
through ``just sync-create-progress-defaults`` to restate the shipped seeds.

The template comes from ``MINDS_CREATE_PROGRESS_TEMPLATE_DIR`` (a local
default-workspace-template checkout, cloned into the test's own directory) or,
when unset, the paired-branch clone the Electron e2e test uses.
"""

import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final
from uuid import uuid4

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.desktop_client.agent_creator import AgentCreateAttemptStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.create_progress import OperationProgressSnapshot
from imbue.minds.desktop_client.create_progress import ProgressStepStatus
from imbue.minds.desktop_client.create_progress_defaults_sync import measurements_from_history_text
from imbue.minds.desktop_client.create_progress_history import PROGRESS_HISTORY_RELATIVE_PATH
from imbue.minds.desktop_client.create_progress_history import StepDurationEvent
from imbue.minds.desktop_client.create_progress_history import create_scope
from imbue.minds.desktop_client.default_workspace_template_worktree import (
    materialize_paired_default_workspace_template_worktree,
)
from imbue.minds.desktop_client.default_workspace_template_worktree import write_pytest_config_opt_in
from imbue.minds.desktop_client.mngr_command import run_mngr_capturing
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.primitives import LaunchMode

pytestmark = [
    pytest.mark.release,
    pytest.mark.docker,
    pytest.mark.timeout(2700),
]

_TEMPLATE_DIR_ENV_VAR: Final[str] = "MINDS_CREATE_PROGRESS_TEMPLATE_DIR"
# A cold template image build (apt, uv, npm, the frontends) dominates the create.
_CREATE_DEADLINE_SECONDS: Final[float] = 2400.0
_SAMPLE_INTERVAL_SECONDS: Final[float] = 2.0
_DESTROY_TIMEOUT_SECONDS: Final[float] = 300.0
_UNMEASURED_STEP_IDS: Final[tuple[str, ...]] = ("clone_repo", "wait_for_ready")
# Under the repo root (the test's own cwd is redirected by its fixtures), where the sync job reads it.
HISTORY_EXPORT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / ".test_output" / "create_progress" / "events.jsonl"
)


def _template_checkout(tmp_path: Path) -> Path:
    """A throwaway template checkout that opts into running under pytest."""
    source = os.environ.get(_TEMPLATE_DIR_ENV_VAR, "")
    if not source:
        return materialize_paired_default_workspace_template_worktree().path
    destination = tmp_path / "template"
    subprocess.run(
        ["git", "clone", "-q", f"file://{Path(source).resolve()}", str(destination)],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    write_pytest_config_opt_in(destination / ".mngr" / "settings.toml")
    subprocess.run(["git", "-C", str(destination), "add", "-A"], check=True, capture_output=True, timeout=60)
    subprocess.run(
        [
            "git",
            "-C",
            str(destination),
            "-c",
            "user.email=test@imbue.com",
            "-c",
            "user.name=test",
            "commit",
            "-q",
            "-m",
            "Opt into pytest",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return destination


def _sample_until_finished(
    read_status: Callable[[], tuple[AgentCreateAttemptStatus, OperationProgressSnapshot | None]],
) -> list[OperationProgressSnapshot]:
    """Every progress snapshot the attempt published, polled the way the creation page polls, until it ends."""
    samples: list[OperationProgressSnapshot] = []
    deadline = time.monotonic() + _CREATE_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        status, snapshot = read_status()
        if snapshot is not None:
            samples.append(snapshot)
        if status in (AgentCreateAttemptStatus.DONE, AgentCreateAttemptStatus.FAILED):
            return samples
        threading.Event().wait(_SAMPLE_INTERVAL_SECONDS)
    raise AssertionError(f"the create did not finish within {_CREATE_DEADLINE_SECONDS:.0f}s")


def test_docker_create_reports_honest_progress_and_records_its_durations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not found")
    template_dir = _template_checkout(tmp_path)
    # The test isolation renames mngr's project config directory; the create
    # must still read the template's own ``.mngr/settings.toml`` (its create
    # templates and the docker build args), so name it explicitly.
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(template_dir / ".mngr"))
    data_dir = tmp_path / "data"
    cg = ConcurrencyGroup(name=f"create-progress-release-{uuid4().hex}")
    host_name = f"progress-{uuid4().hex}"
    with cg:
        creator = AgentCreator(
            paths=InstallationPaths(data_dir=data_dir),
            root_concurrency_group=cg,
            system_interface_health_tracker=SystemInterfaceHealthTracker(),
        )
        try:
            create_attempt_id = creator.start_create_attempt(
                repo_source=str(template_dir),
                host_name=host_name,
                launch_mode=LaunchMode.DOCKER,
            )

            def read_status() -> tuple[AgentCreateAttemptStatus, OperationProgressSnapshot | None]:
                info = creator.get_create_attempt_info(create_attempt_id)
                assert info is not None
                return info.status, info.progress

            samples = _sample_until_finished(read_status)
            info = creator.get_create_attempt_info(create_attempt_id)
            assert info is not None
            assert info.status is AgentCreateAttemptStatus.DONE, info.error

            # The bar only ever moved forward, named what was running, and finished full.
            percents = [sample.percent for sample in samples]
            assert percents == sorted(percents)
            assert percents[-1] == 100.0
            assert all(step.status is ProgressStepStatus.DONE for step in samples[-1].steps)
            assert [step.step_id for step in samples[-1].steps] == ["clone_repo", "mngr_create", "wait_for_ready"]
            labels_seen = {sample.current_label for sample in samples if sample.current_label}
            assert "Building the image" in labels_seen, labels_seen
            assert any(sample.remaining_text for sample in samples)

            # The durations it measured are what the next Docker create learns from.
            history_text = (data_dir / PROGRESS_HISTORY_RELATIVE_PATH).read_text()
            history = measurements_from_history_text(history_text)
            scope = create_scope(LaunchMode.DOCKER)
            measured = history.by_scope[scope]
            assert {"clone_repo", "mngr_create", "creating_host", "docker.build", "wait_for_ready"} <= set(measured)
            assert "docker.build.export" in measured
            # A RUN stage the layer cache served is recorded skipped rather than measured.
            assert any(step.startswith("docker.build.run_") for step in history.seen_by_scope[scope])
            # A COPY stage can finish within the reporter's clock resolution and measure 0.
            assert all(seconds >= 0 for samples_for_step in measured.values() for seconds in samples_for_step)

            # This create reads the template from a local clone and probes no system
            # interface, so those two steps measured nothing here; only what it did
            # measure is worth seeding from.
            exported = [
                line
                for line in history_text.splitlines()
                if line.strip() and StepDurationEvent.model_validate_json(line).step_id not in _UNMEASURED_STEP_IDS
            ]
            HISTORY_EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
            HISTORY_EXPORT_PATH.write_text("\n".join(exported) + "\n")
        finally:
            # By host address on the docker provider, the way the desktop tears a
            # workspace down. The template keeps that provider off outside a create
            # (its docker create template switches it on), so the destroy switches it
            # on the same way; a forced destroy of an address that matches nothing
            # exits clean, hence the check that the container is really gone.
            stdout, returncode, stderr = run_mngr_capturing(
                cg,
                [
                    MNGR_BINARY,
                    "destroy",
                    f"@{host_name}.docker",
                    "--force",
                    "-S",
                    "providers.docker.is_enabled=true",
                ],
                dict(os.environ),
                timeout_seconds=_DESTROY_TIMEOUT_SECONDS,
            )
            assert returncode == 0, f"destroy failed: {stderr}\n{stdout}"
            container_names = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{.Names}}"],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout.split()
            assert not any(name.endswith(host_name) for name in container_names), container_names
