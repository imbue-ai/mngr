from imbue.mngr.primitives import DockerBuilder
from imbue.mngr.providers.docker.config import DockerProviderConfig


def test_builder_defaults_to_docker() -> None:
    """Default is DOCKER -- depot is opt-in via settings.toml."""
    assert DockerProviderConfig().builder is DockerBuilder.DOCKER


def test_explicit_builder_is_honored() -> None:
    """`builder` is a plain config field; the constructor argument wins."""
    assert DockerProviderConfig(builder=DockerBuilder.DEPOT).builder is DockerBuilder.DEPOT


def test_build_timeout_seconds_defaults_to_ten_minutes() -> None:
    """Default build timeout is 10 minutes, matching slower base-image pulls."""
    assert DockerProviderConfig().build_timeout_seconds == 600


def test_explicit_build_timeout_seconds_is_honored() -> None:
    """`build_timeout_seconds` is configurable per provider instance."""
    assert DockerProviderConfig(build_timeout_seconds=1800).build_timeout_seconds == 1800
