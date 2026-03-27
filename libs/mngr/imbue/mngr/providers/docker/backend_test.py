from pathlib import Path

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.docker.backend import DOCKER_BACKEND_NAME
from imbue.mngr.providers.docker.backend import DockerProviderBackend
from imbue.mngr.providers.docker.backend import get_files_for_deploy
from imbue.mngr.providers.docker.config import DockerProviderConfig
from imbue.mngr.providers.docker.instance import DockerProviderInstance


def test_backend_name() -> None:
    assert DockerProviderBackend.get_name() == DOCKER_BACKEND_NAME
    assert DockerProviderBackend.get_name() == ProviderBackendName("docker")


def test_backend_description() -> None:
    desc = DockerProviderBackend.get_description()
    assert isinstance(desc, str)
    assert len(desc) > 0
    assert "docker" in desc.lower()


def test_backend_build_args_help() -> None:
    help_text = DockerProviderBackend.get_build_args_help()
    assert isinstance(help_text, str)
    assert "docker build" in help_text.lower()


def test_backend_start_args_help() -> None:
    help_text = DockerProviderBackend.get_start_args_help()
    assert isinstance(help_text, str)
    assert "docker run" in help_text.lower()


def test_backend_get_config_class() -> None:
    config_class = DockerProviderBackend.get_config_class()
    assert config_class is DockerProviderConfig


def test_build_provider_instance_returns_docker_provider_instance(temp_mngr_ctx: MngrContext) -> None:
    config = DockerProviderConfig()
    instance = DockerProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test-docker"),
        config=config,
        mngr_ctx=temp_mngr_ctx,
    )
    assert isinstance(instance, DockerProviderInstance)


def test_build_provider_instance_with_custom_host_dir(temp_mngr_ctx: MngrContext) -> None:
    config = DockerProviderConfig(host_dir=Path("/custom/dir"))
    instance = DockerProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test-docker"),
        config=config,
        mngr_ctx=temp_mngr_ctx,
    )
    assert instance.host_dir == Path("/custom/dir")


def test_build_provider_instance_uses_default_host_dir(temp_mngr_ctx: MngrContext) -> None:
    config = DockerProviderConfig()
    instance = DockerProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test-docker"),
        config=config,
        mngr_ctx=temp_mngr_ctx,
    )
    assert instance.host_dir == Path("/mngr")


def test_build_provider_instance_uses_name(temp_mngr_ctx: MngrContext) -> None:
    config = DockerProviderConfig()
    instance = DockerProviderBackend.build_provider_instance(
        name=ProviderInstanceName("my-docker"),
        config=config,
        mngr_ctx=temp_mngr_ctx,
    )
    assert instance.name == ProviderInstanceName("my-docker")


# =============================================================================
# get_files_for_deploy Tests
# =============================================================================


def test_get_files_for_deploy_returns_empty_when_user_settings_excluded(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """get_files_for_deploy returns empty dict when include_user_settings is False."""
    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=False, include_project_settings=True, repo_root=tmp_path
    )

    assert result == {}


def test_get_files_for_deploy_returns_empty_when_no_docker_dir(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy returns empty dict when no docker provider directory exists."""
    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=tmp_path
    )

    assert result == {}


def test_get_files_for_deploy_excludes_ssh_key_files(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy excludes SSH key files from the docker provider directory."""
    docker_dir = temp_mngr_ctx.profile_dir / "providers" / "docker" / "default" / "keys"
    docker_dir.mkdir(parents=True)
    (docker_dir / "docker_ssh_key").write_text("private-key-data")
    (docker_dir / "docker_ssh_key.pub").write_text("public-key-data")
    (docker_dir / "known_hosts").write_text("[localhost]:2222 ssh-ed25519 AAAA...")

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=tmp_path
    )

    assert result == {}


def test_get_files_for_deploy_includes_non_key_files(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy includes non-key files from the docker provider directory."""
    docker_dir = temp_mngr_ctx.profile_dir / "providers" / "docker"
    docker_dir.mkdir(parents=True)
    config_file = docker_dir / "config.json"
    config_file.write_text('{"docker": "config"}')

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=tmp_path
    )

    assert len(result) == 1
    matched_values = list(result.values())
    assert matched_values[0] == config_file
