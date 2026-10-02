from ipaddress import IPv4Address
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.loader import load_config
from imbue.mngr.config.provider_config_registry import register_provider_config
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.testing import setup_layered_config_test_env
from imbue.mngr_docker.config import DockerProviderConfig


def test_load_config_accepts_docker_bind_address_unreachable_from_a_remote_daemon_host_set_in_another_layer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """A docker provider whose merged host and bind address cannot work together still loads.

    The lower layer points docker at a remote daemon; the higher layer (shaped like a project
    template's) disables docker and pins a loopback bind. Only creating a docker container can
    fail on that combination, so every other command -- e.g. creating on another provider --
    must still load its config.
    """
    pm, project_dir = setup_layered_config_test_env(monkeypatch, tmp_path)
    register_provider_config("docker", DockerProviderConfig)
    (project_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n"
        '[providers.docker]\nbackend = "docker"\nhost = "ssh://user@daemon-host"\nisolate_host_volumes = true\n'
    )
    (project_dir / "settings.local.toml").write_text(
        "is_allowed_in_pytest = true\n\n[providers.docker]\nis_enabled = false\n"
        'isolate_host_volumes = true\nssh_bind_address = "127.0.0.1"\n'
    )

    mngr_ctx = load_config(pm=pm, concurrency_group=cg)

    docker_config = mngr_ctx.config.providers[ProviderInstanceName("docker")]
    assert isinstance(docker_config, DockerProviderConfig)
    assert docker_config.host == "ssh://user@daemon-host"
    assert docker_config.ssh_bind_address == IPv4Address("127.0.0.1")
    assert docker_config.is_enabled is False
