import pytest

from imbue.minds.primitives import DockerRuntime
from imbue.minds.primitives import default_docker_runtime


def test_default_docker_runtime_is_runc_without_an_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # A fresh install has no gVisor registered on any platform, so the create
    # default must never stack the docker_runsc template on its own.
    monkeypatch.delenv("MINDS_DOCKER_RUNTIME_DEFAULT", raising=False)
    assert default_docker_runtime() is DockerRuntime.RUNC


@pytest.mark.parametrize("value", [DockerRuntime.RUNC, DockerRuntime.RUNSC])
def test_default_docker_runtime_env_override_wins(value: DockerRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_DOCKER_RUNTIME_DEFAULT", value.value)
    assert default_docker_runtime() is value


def test_default_docker_runtime_env_override_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_DOCKER_RUNTIME_DEFAULT", "runc")
    assert default_docker_runtime() is DockerRuntime.RUNC


def test_default_docker_runtime_invalid_override_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # A misconfigured knob must fail loud rather than silently fall back.
    monkeypatch.setenv("MINDS_DOCKER_RUNTIME_DEFAULT", "gvisor")
    with pytest.raises(ValueError):
        default_docker_runtime()
