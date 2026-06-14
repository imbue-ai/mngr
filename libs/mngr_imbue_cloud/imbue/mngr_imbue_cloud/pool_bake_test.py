import json

import pytest

from imbue.mngr_imbue_cloud.pool_bake import BAKED_SERVICES_AGENT_NAME
from imbue.mngr_imbue_cloud.pool_bake import FCT_BAKE_TEMPLATES
from imbue.mngr_imbue_cloud.pool_bake import PoolBakeError
from imbue.mngr_imbue_cloud.pool_bake import build_pool_create_command
from imbue.mngr_imbue_cloud.pool_bake import parse_baked_host


def test_build_pool_create_command_targets_the_given_provider_with_fct_templates() -> None:
    command = build_pool_create_command(
        provider_instance="imbue_cloud_slice",
        host_name="slice-abc",
        attributes_json='{"cpus": 3}',
        extra_args=["-S", "providers.imbue_cloud_slice.slice_vcpus=3"],
    )
    # Address carries the constant services agent name + per-bake host + provider.
    assert command[1] == f"{BAKED_SERVICES_AGENT_NAME}@slice-abc.imbue_cloud_slice"
    # Both FCT bake templates are stacked, and the result is machine-readable.
    for template in FCT_BAKE_TEMPLATES:
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
