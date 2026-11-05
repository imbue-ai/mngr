import pytest

from imbue.minds.build_info import FALLBACK_BRANCH
from imbue.minds.errors import InvalidTemplateReleaseTagError
from imbue.minds.primitives import DockerRuntime
from imbue.minds.primitives import TemplateReleaseTag
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


@pytest.mark.parametrize(
    "value", ["minds-v0.8.7", "minds-v10.0.123", " minds-v1.2.3 ", "minds-v0.9.0-rc.1", "minds-v0.9.0-beta-2"]
)
def test_template_release_tag_accepts_minds_release_and_prerelease_tags(value: str) -> None:
    assert TemplateReleaseTag(value) == value.strip()


@pytest.mark.parametrize(
    "value",
    [
        "main",
        "v0.8.7",
        "minds-v0.8",
        "minds-v0.8.7rc1",
        "minds-v0.8.7-",
        "minds-v0.8.7-rc..1",
        "minds-v0.8.7-rc/1",
        "284f548c21eb6996a4018e54571b6390e1038890",
    ],
)
def test_template_release_tag_rejects_branches_shas_and_other_tags(value: str) -> None:
    with pytest.raises(InvalidTemplateReleaseTagError):
        TemplateReleaseTag(value)


def test_the_pinned_template_release_is_a_release_tag() -> None:
    # A pin that is not a release tag turns off the app's template cache at launch, so catch it here.
    assert TemplateReleaseTag(FALLBACK_BRANCH) == FALLBACK_BRANCH
