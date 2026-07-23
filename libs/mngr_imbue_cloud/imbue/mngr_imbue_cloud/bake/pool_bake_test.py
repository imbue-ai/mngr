import json

import pytest

from imbue.mngr_imbue_cloud.bake.pool_bake import BAKED_SERVICES_AGENT_NAME
from imbue.mngr_imbue_cloud.bake.pool_bake import BakedPoolHost
from imbue.mngr_imbue_cloud.bake.pool_bake import DEFAULT_WORKSPACE_TEMPLATE_BAKE_TEMPLATES
from imbue.mngr_imbue_cloud.bake.pool_bake import PoolBakeError
from imbue.mngr_imbue_cloud.bake.pool_bake import build_pool_create_command
from imbue.mngr_imbue_cloud.bake.pool_bake import finalize_baked_pool_host
from imbue.mngr_imbue_cloud.bake.pool_bake import parse_baked_host
from imbue.mngr_imbue_cloud.bake.pool_bake import wait_for_deferred_install


class _ScriptedRunner:
    """A ContainerCommandRunner that returns a scripted ``(rc, out, err)`` per step label.

    Lets finalize_baked_pool_host be unit-tested without a real container: each call
    records its (label, command) and returns the response scripted for that label
    (default ``(0, "", "")``).
    """

    def __init__(self, responses: dict[str, tuple[int | None, str, str]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, baked: BakedPoolHost, label: str, command: str, timeout_seconds: float
    ) -> tuple[int | None, str, str]:
        self.calls.append((label, command))
        return self.responses.get(label, (0, "", ""))


def _baked() -> BakedPoolHost:
    return BakedPoolHost(agent_id="a", host_id="h", host_name="slice-x", ssh_host="1.2.3.4", ssh_port=22001)


def test_finalize_tears_down_chat_agent_when_sentinel_present() -> None:
    # All steps succeed; the sentinel-wait returns 0, i.e. the sentinel is present.
    runner = _ScriptedRunner({})
    finalize_baked_pool_host(runner, _baked(), host_name="slice-x", sentinel_timeout_seconds=5)
    labels = [label for label, _cmd in runner.calls]
    assert labels == ["sshd-harden", "git-identity-reset", "sentinel-wait", "chat-destroy", "sentinel-rm"]
    destroy_cmd = next(cmd for label, cmd in runner.calls if label == "chat-destroy")
    assert "uv run mngr destroy" in destroy_cmd and "slice-x" in destroy_cmd


def test_finalize_clears_baked_git_identity() -> None:
    # The bake copies the operator's git identity into /mngr/code; finalize must
    # unset it so adopting users' agents don't inherit the baker as their commit
    # author (the bootstrap re-supplies its neutral fallback on adoption).
    runner = _ScriptedRunner({})
    finalize_baked_pool_host(runner, _baked(), host_name="slice-x", sentinel_timeout_seconds=5)
    reset_cmd = next(cmd for label, cmd in runner.calls if label == "git-identity-reset")
    assert "git -C /mngr/code config --local --unset user.name" in reset_cmd
    assert "git -C /mngr/code config --local --unset user.email" in reset_cmd
    # It does not substitute any hardcoded identity value.
    assert "minds-bootstrap" not in reset_cmd
    # An already-absent key (git config --unset exit 5) is tolerated, not a failure.
    assert "[ $? -eq 5 ]" in reset_cmd
    # It runs before the sentinel wait, so it applies even when no chat agent exists.
    labels = [label for label, _cmd in runner.calls]
    assert labels.index("git-identity-reset") < labels.index("sentinel-wait")


def test_finalize_git_identity_reset_failure_is_best_effort() -> None:
    # The rewrite hook is the authoritative per-agent attribution, so a failed
    # identity reset is logged, not fatal, and teardown still proceeds.
    runner = _ScriptedRunner({"git-identity-reset": (1, "", "boom")})
    finalize_baked_pool_host(runner, _baked(), host_name="slice-x", sentinel_timeout_seconds=5)
    assert "chat-destroy" in [label for label, _cmd in runner.calls]


def test_finalize_still_resets_git_identity_when_no_chat_agent() -> None:
    # Even when the sentinel times out (no chat agent), the identity reset must
    # have already run, since it precedes the sentinel wait.
    runner = _ScriptedRunner({"sentinel-wait": (124, "", "")})
    finalize_baked_pool_host(runner, _baked(), host_name="slice-x", sentinel_timeout_seconds=5)
    labels = [label for label, _cmd in runner.calls]
    assert "git-identity-reset" in labels
    assert "chat-destroy" not in labels


def test_finalize_skips_teardown_on_sentinel_timeout() -> None:
    # timeout exit 124 => bootstrap never made a chat agent => nothing to tear down.
    runner = _ScriptedRunner({"sentinel-wait": (124, "", "")})
    finalize_baked_pool_host(runner, _baked(), host_name="slice-x", sentinel_timeout_seconds=5)
    labels = [label for label, _cmd in runner.calls]
    assert "chat-destroy" not in labels


def test_finalize_raises_on_transport_error_during_sentinel_wait() -> None:
    # A non-timeout failure (e.g. ssh exit 255) must NOT be silently skipped:
    # that would ship a pool host with a stale bootstrap chat agent.
    runner = _ScriptedRunner({"sentinel-wait": (255, "", "ssh: connect failed")})
    with pytest.raises(PoolBakeError):
        finalize_baked_pool_host(runner, _baked(), host_name="slice-x", sentinel_timeout_seconds=5)


def test_finalize_sshd_harden_failure_is_best_effort() -> None:
    # sshd-harden is best-effort: a failure is logged, not fatal, and teardown proceeds.
    runner = _ScriptedRunner({"sshd-harden": (1, "", "boom")})
    finalize_baked_pool_host(runner, _baked(), host_name="slice-x", sentinel_timeout_seconds=5)
    assert "chat-destroy" in [label for label, _cmd in runner.calls]


def test_finalize_raises_when_chat_destroy_fails() -> None:
    runner = _ScriptedRunner({"chat-destroy": (1, "", "skew")})
    with pytest.raises(PoolBakeError):
        finalize_baked_pool_host(runner, _baked(), host_name="slice-x", sentinel_timeout_seconds=5)


def test_wait_for_deferred_install_polls_for_marker_or_finished_process() -> None:
    runner = _ScriptedRunner({})
    wait_for_deferred_install(runner, _baked(), host_name="slice-x", timeout_seconds=5)
    assert [label for label, _cmd in runner.calls] == ["deferred-install-wait"]
    command = runner.calls[0][1]
    # The poll checks the success marker and uses a bracketed pgrep pattern (self-match guard).
    assert "done.playwright" in command
    assert "[d]eferred_install.sh" in command
    assert "timeout 5" in command


def test_wait_for_deferred_install_is_best_effort_on_timeout() -> None:
    # Hitting the cap (timeout exit 124) must not fail the bake; it retries on lease.
    runner = _ScriptedRunner({"deferred-install-wait": (124, "", "")})
    wait_for_deferred_install(runner, _baked(), host_name="slice-x", timeout_seconds=5)


def test_wait_for_deferred_install_is_best_effort_on_transport_error() -> None:
    runner = _ScriptedRunner({"deferred-install-wait": (255, "", "ssh: connect failed")})
    wait_for_deferred_install(runner, _baked(), host_name="slice-x", timeout_seconds=5)


def test_build_pool_create_command_targets_the_given_provider_with_default_workspace_templates() -> None:
    command = build_pool_create_command(
        provider_instance="imbue_cloud_slice",
        host_name="slice-abc",
        attributes_json='{"cpus": 3}',
        extra_args=["-S", "providers.imbue_cloud_slice.slice_vcpus=3"],
    )
    # Address carries the constant services agent name + per-bake host + provider.
    assert command[1] == f"{BAKED_SERVICES_AGENT_NAME}@slice-abc.imbue_cloud_slice"
    # Both DEFAULT_WORKSPACE_TEMPLATE bake templates are stacked, and the result is machine-readable.
    for template in DEFAULT_WORKSPACE_TEMPLATE_BAKE_TEMPLATES:
        assert template in command
    assert "--format" in command and "json" in command
    # The pool attributes ride along as a label, and extra args are appended verbatim.
    assert 'pool_attributes={"cpus": 3}' in command
    assert command[-2:] == ["-S", "providers.imbue_cloud_slice.slice_vcpus=3"]


def test_build_pool_create_command_for_ovh_appends_backend_args() -> None:
    command = build_pool_create_command(
        provider_instance="ovh",
        host_name="pool-xyz-host",
        attributes_json="{}",
        extra_args=["-b", "--ovh-datacenter=vin"],
    )
    assert command[1] == f"{BAKED_SERVICES_AGENT_NAME}@pool-xyz-host.ovh"
    assert command[-2:] == ["-b", "--ovh-datacenter=vin"]


def test_parse_baked_host_reads_all_fields_from_create_json() -> None:
    stdout = (
        "some build log line on stdout that is not json\n"
        + json.dumps(
            {
                "agent_id": "agent-1",
                "host_id": "host-1",
                "host_name": "slice-abc",
                "ssh_user": "root",
                "ssh_host": "15.0.0.1",
                "ssh_port": 22002,
                "ssh_key_path": "/keys/container_ssh_key",
                "outer_ssh_port": 22001,
            }
        )
        + "\n"
    )
    baked = parse_baked_host(stdout, host_name="slice-abc")
    assert baked.agent_id == "agent-1"
    assert baked.host_id == "host-1"
    assert baked.host_name == "slice-abc"
    assert baked.ssh_host == "15.0.0.1"
    assert baked.ssh_port == 22002
    assert baked.ssh_key_path == "/keys/container_ssh_key"
    assert baked.outer_ssh_port == 22001


def test_parse_baked_host_tolerates_absent_outer_port_for_ovh() -> None:
    # OVH has no separate outer/management sshd, so outer_ssh_port is absent.
    stdout = json.dumps({"agent_id": "a", "host_id": "h", "ssh_host": "vps.ovh.us", "ssh_port": 2222})
    baked = parse_baked_host(stdout, host_name="pool-1-host")
    assert baked.outer_ssh_port is None
    assert baked.ssh_port == 2222
    # host_name falls back to the bake's name when the JSON omits it.
    assert baked.host_name == "pool-1-host"


def test_parse_baked_host_raises_when_no_json_present() -> None:
    with pytest.raises(PoolBakeError):
        parse_baked_host("only logs here, no json object\n", host_name="x")


def test_parse_baked_host_raises_when_host_id_missing() -> None:
    with pytest.raises(PoolBakeError):
        parse_baked_host(json.dumps({"agent_id": "a"}), host_name="x")
