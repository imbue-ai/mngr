from click.testing import CliRunner

from imbue.mngr.cli.doc_links import imbue_mngr_doc_url
from imbue.mngr_docker.backend import DockerProviderBackend
from imbue.mngr_docker.config import DockerProviderConfig
from imbue.mngr_docker.constants import DOCKER_BACKEND_NAME
from imbue.mngr_docker.plugin import register_cli_commands
from imbue.mngr_docker.plugin import register_help_topics
from imbue.mngr_docker.plugin import register_provider_backend


def test_register_provider_backend_lazily_resolves_to_the_docker_backend() -> None:
    registration = register_provider_backend()

    assert registration.name == DOCKER_BACKEND_NAME
    assert registration.config_class is DockerProviderConfig
    assert registration.load() is DockerProviderBackend


def test_register_help_topics_serves_the_docker_usage_page_shipped_inside_the_package() -> None:
    (topic,) = register_help_topics()

    assert topic.key == "docker_usage"
    # load_body reads the page from disk, so this also proves it ships with the package.
    assert topic.load_body().startswith("# Using Docker")
    # Pinned to the installed release like every other in-repo topic, so relative links resolve to the shipped docs.
    assert topic.link_base_url() == imbue_mngr_doc_url("libs/mngr_docker/imbue/mngr_docker/docs/docker_usage.md")


def test_register_cli_commands_exposes_resize_under_the_docker_group(cli_runner: CliRunner) -> None:
    (docker_group,) = register_cli_commands()

    assert docker_group.name == "docker"
    result = cli_runner.invoke(docker_group, ["--help"])
    assert result.exit_code == 0, result.output
    assert "resize" in result.output
