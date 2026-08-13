import subprocess
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from inline_snapshot import snapshot
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.ssh_key_migration import ClientKeyType
from imbue.minds.desktop_client.ssh_key_migration import MigratableWorkspace
from imbue.minds.desktop_client.ssh_key_migration import MigrationOutcome
from imbue.minds.desktop_client.ssh_key_migration import SshKeyMigrationError
from imbue.minds.desktop_client.ssh_key_migration import SshKeyMigrationScheduler
from imbue.minds.desktop_client.ssh_key_migration import _generate_ed25519_keypair_openssh
from imbue.minds.desktop_client.ssh_key_migration import build_ensure_authorized_key_command
from imbue.minds.desktop_client.ssh_key_migration import classify_client_private_key
from imbue.minds.desktop_client.ssh_key_migration import find_per_host_key_path
from imbue.minds.desktop_client.ssh_key_migration import list_migratable_workspaces_from_mngr_ls_json
from imbue.minds.desktop_client.ssh_key_migration import migrate_host_client_key
from imbue.minds.desktop_client.ssh_key_migration import run_ssh_key_migration_pass
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller


def _generate_rsa_private_key_pem() -> str:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


class _TwoLayerExecutingMngrCaller(MngrCaller):
    """Caller that runs each exec'd command under bash against per-layer fake HOMEs.

    ``mngr exec`` argv is ``["exec", <agent>, <command>, ...flags]``; the
    command runs with ``HOME`` pointed at ``container_home`` (plain exec) or
    ``outer_home`` (``--outer``), so ``~/.ssh/authorized_keys`` behaves exactly
    as it would on each real layer, without any mngr or container.
    """

    container_home: Path = Field(frozen=True, description="Fake HOME for the container layer")
    outer_home: Path = Field(frozen=True, description="Fake HOME for the outer-host layer")
    failing_command: str | None = Field(
        default=None, description="When set, any exec of exactly this command fails with exit 1"
    )
    failing_outer_command: str | None = Field(
        default=None, description="When set, an --outer exec of exactly this command fails with exit 1"
    )

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> MngrCallResult:
        command = argv[2]
        is_outer = "--outer" in argv
        if self.failing_command is not None and command == self.failing_command:
            return MngrCallResult(returncode=1, stderr="injected failure")
        if is_outer and self.failing_outer_command is not None and command == self.failing_outer_command:
            return MngrCallResult(returncode=1, stderr="injected outer failure")
        home = self.outer_home if is_outer else self.container_home
        completed = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        )
        return MngrCallResult(returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)


class _OuterFailingMngrCaller(_TwoLayerExecutingMngrCaller):
    """Two-layer caller whose outer layer rejects every command."""

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> MngrCallResult:
        if "--outer" in argv:
            return MngrCallResult(returncode=1, stderr="no outer for you")
        return super().call(argv, timeout=timeout, env_overrides=env_overrides, cwd=cwd)


def _make_key_dir_with_rsa_pair(tmp_path: Path, host_id: str) -> Path:
    key_dir = tmp_path / "hosts" / host_id
    key_dir.mkdir(parents=True)
    key_path = key_dir / "ssh_key"
    private_key_pem = _generate_rsa_private_key_pem()
    key_path.write_text(private_key_pem)
    (key_dir / "ssh_key.pub").write_text("ssh-rsa AAAAfake old-key\n")
    return key_path


def _make_two_layer_caller(tmp_path: Path) -> _TwoLayerExecutingMngrCaller:
    container_home = tmp_path / "container-home"
    outer_home = tmp_path / "outer-home"
    container_home.mkdir()
    outer_home.mkdir()
    return _TwoLayerExecutingMngrCaller(container_home=container_home, outer_home=outer_home)


def test_classify_client_private_key_recognizes_rsa_pem() -> None:
    assert classify_client_private_key(_generate_rsa_private_key_pem()) == ClientKeyType.RSA


def test_classify_client_private_key_recognizes_openssh_ed25519() -> None:
    private_key_text, _public = _generate_ed25519_keypair_openssh()
    assert classify_client_private_key(private_key_text) == ClientKeyType.ED25519


def test_classify_client_private_key_reports_garbage_as_unreadable() -> None:
    assert classify_client_private_key("not a key at all") == ClientKeyType.UNREADABLE


def test_build_ensure_authorized_key_command_shape() -> None:
    assert build_ensure_authorized_key_command("ssh-ed25519 AAAA test") == snapshot(
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && { grep -qxF 'ssh-ed25519 AAAA test' ~/.ssh/authorized_keys || printf '%s\\n' 'ssh-ed25519 AAAA test' >> ~/.ssh/authorized_keys; } && grep -qxF 'ssh-ed25519 AAAA test' ~/.ssh/authorized_keys"
    )


def test_ensure_authorized_key_command_is_idempotent_under_bash(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    command = build_ensure_authorized_key_command("ssh-ed25519 AAAAtest idempotence-check")
    for _ in range(2):
        completed = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        )
        assert completed.returncode == 0, completed.stderr
    authorized_lines = (home / ".ssh" / "authorized_keys").read_text().splitlines()
    assert authorized_lines == ["ssh-ed25519 AAAAtest idempotence-check"]


def test_migrate_host_client_key_authorizes_both_layers_and_swaps_to_ed25519(tmp_path: Path) -> None:
    host_id = f"host-{uuid4().hex}"
    key_path = _make_key_dir_with_rsa_pair(tmp_path, host_id)
    original_rsa_text = key_path.read_text()
    caller = _make_two_layer_caller(tmp_path)

    migrate_host_client_key(host_id, "agent-1", key_path, caller)

    # The local pair is now Ed25519, with the RSA originals preserved.
    assert classify_client_private_key(key_path.read_text()) == ClientKeyType.ED25519
    new_public_line = key_path.with_name("ssh_key.pub").read_text().strip()
    assert new_public_line.startswith("ssh-ed25519 ")
    assert key_path.with_name("ssh_key.rsa-backup").read_text() == original_rsa_text
    assert key_path.with_name("ssh_key.pub.rsa-backup").read_text() == "ssh-rsa AAAAfake old-key\n"
    # Both layers' authorized_keys carry exactly the new public key.
    for home in (caller.container_home, caller.outer_home):
        assert (home / ".ssh" / "authorized_keys").read_text().splitlines() == [new_public_line]


def test_migrate_host_client_key_failure_on_outer_leaves_local_files_untouched(tmp_path: Path) -> None:
    host_id = f"host-{uuid4().hex}"
    key_path = _make_key_dir_with_rsa_pair(tmp_path, host_id)
    original_rsa_text = key_path.read_text()
    caller = _make_two_layer_caller(tmp_path)
    outer_failing_caller = _OuterFailingMngrCaller(container_home=caller.container_home, outer_home=caller.outer_home)
    with pytest.raises(SshKeyMigrationError, match="outer host"):
        migrate_host_client_key(host_id, "agent-1", key_path, outer_failing_caller)

    assert key_path.read_text() == original_rsa_text
    assert not key_path.with_name("ssh_key.rsa-backup").exists()


def test_migrate_host_client_key_rolls_back_when_post_swap_probe_fails(tmp_path: Path) -> None:
    host_id = f"host-{uuid4().hex}"
    key_path = _make_key_dir_with_rsa_pair(tmp_path, host_id)
    original_rsa_text = key_path.read_text()
    caller = _make_two_layer_caller(tmp_path)
    probe_failing_caller = _TwoLayerExecutingMngrCaller(
        container_home=caller.container_home,
        outer_home=caller.outer_home,
        # The post-swap connectivity probe is exactly `true`; failing it
        # simulates a host that rejects the freshly-installed key.
        failing_command="true",
    )

    with pytest.raises(SshKeyMigrationError, match="rolled back"):
        migrate_host_client_key(host_id, "agent-1", key_path, probe_failing_caller)

    assert key_path.read_text() == original_rsa_text
    assert key_path.with_name("ssh_key.pub").read_text() == "ssh-rsa AAAAfake old-key\n"


def _make_mngr_profile_with_host_key(tmp_path: Path, host_id: str, private_key_text: str) -> Path:
    """Build a minimal mngr host dir holding one per-host client key; returns the key path."""
    mngr_host_dir = tmp_path / "mngr"
    profile_dir = mngr_host_dir / "profiles" / "p1"
    (mngr_host_dir).mkdir(parents=True)
    (mngr_host_dir / "config.toml").write_text('profile = "p1"\n')
    key_dir = profile_dir / "providers" / "imbue_cloud" / "acct" / "hosts" / host_id
    key_dir.mkdir(parents=True)
    key_path = key_dir / "ssh_key"
    key_path.write_text(private_key_text)
    (key_dir / "ssh_key.pub").write_text("ssh-rsa AAAAfake old-key\n")
    return key_path


def test_find_per_host_key_path_resolves_the_imbue_cloud_layout(tmp_path: Path) -> None:
    host_id = f"host-{uuid4().hex}"
    key_path = _make_mngr_profile_with_host_key(tmp_path, host_id, _generate_rsa_private_key_pem())
    assert find_per_host_key_path(tmp_path / "mngr", host_id) == key_path
    assert find_per_host_key_path(tmp_path / "mngr", f"host-{uuid4().hex}") is None


def test_migration_pass_migrates_running_rsa_host_and_marks_it_done(tmp_path: Path) -> None:
    host_id = f"host-{uuid4().hex}"
    _make_mngr_profile_with_host_key(tmp_path, host_id, _generate_rsa_private_key_pem())
    caller = _make_two_layer_caller(tmp_path)
    marker_dir = tmp_path / "markers"
    workspaces = [MigratableWorkspace(host_id=host_id, agent_id="agent-1", is_running=True)]

    results = run_ssh_key_migration_pass(
        workspaces=workspaces,
        mngr_host_dir=tmp_path / "mngr",
        marker_dir=marker_dir,
        mngr_caller=caller,
        attempt_count_by_host_id={},
    )

    assert [result.outcome for result in results] == [MigrationOutcome.MIGRATED]
    assert (marker_dir / host_id).read_text().startswith("MIGRATED ")
    # A second pass skips the migrated host entirely.
    second_results = run_ssh_key_migration_pass(
        workspaces=workspaces,
        mngr_host_dir=tmp_path / "mngr",
        marker_dir=marker_dir,
        mngr_caller=caller,
        attempt_count_by_host_id={},
    )
    assert second_results == []


def test_migration_pass_marks_ed25519_hosts_without_touching_them(tmp_path: Path) -> None:
    host_id = f"host-{uuid4().hex}"
    private_key_text, _public = _generate_ed25519_keypair_openssh()
    key_path = _make_mngr_profile_with_host_key(tmp_path, host_id, private_key_text)
    caller = _make_two_layer_caller(tmp_path)
    marker_dir = tmp_path / "markers"

    results = run_ssh_key_migration_pass(
        workspaces=[MigratableWorkspace(host_id=host_id, agent_id="agent-1", is_running=True)],
        mngr_host_dir=tmp_path / "mngr",
        marker_dir=marker_dir,
        mngr_caller=caller,
        attempt_count_by_host_id={},
    )

    assert [result.outcome for result in results] == [MigrationOutcome.ALREADY_ED25519]
    assert (marker_dir / host_id).read_text().startswith("ALREADY_ED25519 ")
    assert key_path.read_text() == private_key_text
    assert not (caller.container_home / ".ssh").exists()


def test_migration_pass_skips_stopped_hosts_and_hosts_without_per_host_keys(tmp_path: Path) -> None:
    stopped_host_id = f"host-{uuid4().hex}"
    keyless_host_id = f"host-{uuid4().hex}"
    _make_mngr_profile_with_host_key(tmp_path, stopped_host_id, _generate_rsa_private_key_pem())
    caller = _make_two_layer_caller(tmp_path)
    marker_dir = tmp_path / "markers"

    results = run_ssh_key_migration_pass(
        workspaces=[
            MigratableWorkspace(host_id=stopped_host_id, agent_id="agent-1", is_running=False),
            MigratableWorkspace(host_id=keyless_host_id, agent_id="agent-2", is_running=True),
        ],
        mngr_host_dir=tmp_path / "mngr",
        marker_dir=marker_dir,
        mngr_caller=caller,
        attempt_count_by_host_id={},
    )

    outcome_by_host_id = {result.host_id: result.outcome for result in results}
    assert outcome_by_host_id == {
        stopped_host_id: MigrationOutcome.SKIPPED_NOT_RUNNING,
        keyless_host_id: MigrationOutcome.SKIPPED_NO_PER_HOST_KEY,
    }
    assert not (marker_dir / stopped_host_id).exists()
    assert not (marker_dir / keyless_host_id).exists()


def test_migration_pass_caps_attempts_for_a_persistently_failing_host(tmp_path: Path) -> None:
    host_id = f"host-{uuid4().hex}"
    _make_mngr_profile_with_host_key(tmp_path, host_id, _generate_rsa_private_key_pem())
    caller = _make_two_layer_caller(tmp_path)
    failing_caller = _TwoLayerExecutingMngrCaller(
        container_home=caller.container_home,
        outer_home=caller.outer_home,
        failing_command="true",
    )
    marker_dir = tmp_path / "markers"
    attempt_count_by_host_id: dict[str, int] = {}
    workspaces = [MigratableWorkspace(host_id=host_id, agent_id="agent-1", is_running=True)]

    outcomes = []
    for _ in range(4):
        results = run_ssh_key_migration_pass(
            workspaces=workspaces,
            mngr_host_dir=tmp_path / "mngr",
            marker_dir=marker_dir,
            mngr_caller=failing_caller,
            attempt_count_by_host_id=attempt_count_by_host_id,
        )
        outcomes.append(results[0].outcome)

    assert outcomes == [
        MigrationOutcome.FAILED,
        MigrationOutcome.FAILED,
        MigrationOutcome.FAILED,
        MigrationOutcome.SKIPPED_ATTEMPTS_EXHAUSTED,
    ]
    assert not (marker_dir / host_id).exists()


def test_list_migratable_workspaces_from_mngr_ls_json_dedupes_hosts_and_reads_state() -> None:
    json_text = (
        '{"agents": ['
        '{"id": "agent-1", "host": {"id": "host-aa", "state": "RUNNING"}},'
        '{"id": "agent-2", "host": {"id": "host-aa", "state": "RUNNING"}},'
        '{"id": "agent-3", "host": {"id": "host-bb", "state": "STOPPED"}},'
        '{"id": "agent-4"}'
        "]}"
    )
    workspaces = list_migratable_workspaces_from_mngr_ls_json(json_text)
    assert workspaces == [
        MigratableWorkspace(host_id="host-aa", agent_id="agent-1", is_running=True),
        MigratableWorkspace(host_id="host-bb", agent_id="agent-3", is_running=False),
    ]


def test_list_migratable_workspaces_from_mngr_ls_json_raises_on_garbage() -> None:
    with pytest.raises(SshKeyMigrationError):
        list_migratable_workspaces_from_mngr_ls_json("this is not json")


def test_scheduler_starts_its_loop_thread_and_stops_cleanly(tmp_path: Path) -> None:
    # Guards the start_new_thread call signature: a keyword mismatch there
    # raises TypeError inside start() and would crash `minds run` at startup.
    scheduler = SshKeyMigrationScheduler(
        resolver=StaticBackendResolver(url_by_agent_and_service={}),
        mngr_caller=_make_two_layer_caller(tmp_path),
        mngr_host_dir=tmp_path / "mngr",
        marker_dir=tmp_path / "markers",
    )
    with ConcurrencyGroup(name="test-ssh-key-migration") as concurrency_group:
        scheduler.start(concurrency_group)
        scheduler.stop(wait_timeout_seconds=30.0)
        # The loop observed the stop signal and exited (stop would have logged
        # and returned even on a hang, so assert the exit event directly).
        assert scheduler._exited_event.is_set()
