import json

import pytest

from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.slice_bake import build_chat_teardown_container_command
from imbue.mngr_imbue_cloud.slice_bake import build_slice_bake_remote_command
from imbue.mngr_imbue_cloud.slice_bake import build_wait_for_sentinel_container_command
from imbue.mngr_imbue_cloud.slice_bake import parse_create_json_from_output


def _bake_command() -> str:
    return build_slice_bake_remote_command(
        fct_dir="/home/limahost/forever-claude-template",
        mngr_bin="/home/limahost/mngr/.venv/bin/mngr",
        host_name="slice-abc",
        attributes_json='{"memory_gb": 8, "cpus": 3}',
        box_public_address="15.204.140.221",
        pool_public_key="ssh-ed25519 AAAAC3pool key-comment",
        slice_vcpus=3,
    )


def test_bake_command_targets_slice_provider_with_shared_ovh_template() -> None:
    command = _bake_command()
    assert "system-services@slice-abc.imbue_cloud_slice" in command
    assert "--template main --template ovh" in command
    assert "--format json" in command


def test_bake_command_injects_box_address_and_pool_key_via_setting_overrides() -> None:
    command = _bake_command()
    assert "providers.imbue_cloud_slice.box_public_address=15.204.140.221" in command
    assert "providers.imbue_cloud_slice.pool_authorized_public_key=ssh-ed25519 AAAAC3pool key-comment" in command


def test_bake_command_sets_vm_vcpus_to_match_advertised_cpus() -> None:
    command = _bake_command()
    assert "providers.imbue_cloud_slice.slice_vcpus=3" in command


def test_bake_command_points_at_fct_config_dir_so_templates_load_without_git() -> None:
    # The synced FCT workspace has no .git, so the project-config dir must be
    # passed explicitly or mngr would not find the create templates.
    command = _bake_command()
    assert "MNGR_PROJECT_CONFIG_DIR=/home/limahost/forever-claude-template/.mngr" in command
    assert command.startswith("cd /home/limahost/forever-claude-template &&")


def test_chat_teardown_destroys_named_agent_and_removes_sentinel() -> None:
    command = build_chat_teardown_container_command("slice-abc")
    assert "mngr destroy slice-abc --force" in command
    assert "/code/runtime/initial_chat_created" in command


def test_wait_for_sentinel_uses_remote_timeout_loop() -> None:
    command = build_wait_for_sentinel_container_command(300)
    assert "timeout 300" in command
    assert "until test -f" in command
    assert "/code/runtime/initial_chat_created" in command


def test_parse_create_json_returns_object_with_host_id() -> None:
    stdout = "build log line\n" + json.dumps(
        {"host_id": "host-x", "agent_id": "agent-y", "ssh_port": 22001, "outer_ssh_port": 22000}
    )
    parsed = parse_create_json_from_output(stdout)
    assert parsed["host_id"] == "host-x"
    assert parsed["outer_ssh_port"] == 22000


def test_parse_create_json_raises_when_no_json_object_present() -> None:
    with pytest.raises(BareMetalProvisioningError, match="no create --format json"):
        parse_create_json_from_output("just logs\nno json here\n")


def test_parse_create_json_raises_when_object_missing_host_id() -> None:
    with pytest.raises(BareMetalProvisioningError, match="missing host_id"):
        parse_create_json_from_output(json.dumps({"agent_id": "agent-y"}))


def test_parse_create_json_raises_on_malformed_json_line() -> None:
    with pytest.raises(BareMetalProvisioningError, match="not valid JSON"):
        parse_create_json_from_output('{"host_id": "x", bad}')
