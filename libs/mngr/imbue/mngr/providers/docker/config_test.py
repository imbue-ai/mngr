from imbue.mngr.primitives import DockerBuilder
from imbue.mngr.providers.docker.config import DockerProviderConfig


def test_builder_defaults_to_docker() -> None:
    """Default is DOCKER -- depot is opt-in via settings.toml."""
    assert DockerProviderConfig().builder is DockerBuilder.DOCKER


def test_explicit_builder_is_honored() -> None:
    """`builder` is a plain config field; the constructor argument wins."""
    assert DockerProviderConfig(builder=DockerBuilder.DEPOT).builder is DockerBuilder.DEPOT
