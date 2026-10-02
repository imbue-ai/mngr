import pytest

from imbue.mngr.utils.env_utils import TEST_ENV_PREFIX
from imbue.mngr_docker.leaked_container_sweep import TEST_PROVIDER_NAME_PREFIX
from imbue.mngr_docker.leaked_container_sweep import _is_test_container


@pytest.mark.parametrize(
    ("provider_name", "container_name", "is_expected_test_container"),
    [
        # The provider name make_docker_provider_with_cleanup assigns.
        (f"{TEST_PROVIDER_NAME_PREFIX}ab12", "anything", True),
        # The environment name generate_test_environment_name assigns.
        ("docker", f"{TEST_ENV_PREFIX}2026-01-01-00-00-00-abcdef12-my-host", True),
        # The autouse per-test prefix (mngr_ plus a hex UUID).
        ("docker", "mngr_22921e597952421296c8973d922f2eb3-docker-state-user", True),
        # A user's real container: default provider name and the plain mngr- prefix.
        ("docker", "mngr-my-host", False),
        ("docker", "mngr-docker-state-715245b5075646fb8b55ca949a291049", False),
        # A prefix that merely resembles the per-test one is not matched.
        ("docker", "mngr_not-hex-my-host", False),
    ],
)
def test_is_test_container_matches_only_test_originated_containers(
    provider_name: str, container_name: str, is_expected_test_container: bool
) -> None:
    assert _is_test_container(provider_name, container_name) is is_expected_test_container
