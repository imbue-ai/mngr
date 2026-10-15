import ast
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.errors import MngrError
from imbue.mngr_modal.backend import STATE_VOLUME_FS_VERSION
from imbue.mngr_modal.routes.deployment import ensure_function_deployed
from imbue.mngr_modal.routes.deployment import get_route_script_path
from imbue.mngr_modal.routes.deployment import get_source_marker_function_name
from imbue.modal_proxy.errors import ModalProxyAppLockedError
from imbue.modal_proxy.errors import ModalProxyDeployRetryableError
from imbue.modal_proxy.errors import ModalProxyDeployTimeoutError
from imbue.modal_proxy.errors import ModalProxyError
from imbue.modal_proxy.errors import ModalProxyImageBuilderLostError
from imbue.modal_proxy.errors import ModalProxyInvalidError
from imbue.modal_proxy.errors import ModalProxyNotFoundError
from imbue.modal_proxy.testing import FakeModalInterface

_FUNCTION = "snapshot_and_shutdown"
_APP = "mngr-test-app"
_ENV = "mngr_test-2026-05-20-12-00-00-shared-abc123def456"


def _publish(interface: FakeModalInterface, app_name: str, url: str) -> None:
    """Publish the route function and its source marker, as a finished deploy would."""
    interface.register_deployed_function(_FUNCTION, app_name=app_name, url=url)
    interface.register_deployed_function(get_source_marker_function_name(_FUNCTION), app_name=app_name)


class _AppLockedUntilAnotherDeployWinsInterface(FakeModalInterface):
    """Refuses the first deploy as app-locked, publishing this source meanwhile.

    Stands in for the cold-app race: several creates against one app all find
    the source marker missing and all deploy, Modal serializes them, and the
    losers are refused while the winner publishes the very source they were
    about to deploy.
    """

    deploy_attempts: int = Field(default=0, description="How many deploys this interface was asked to perform")
    winner_url: str = Field(default="https://winner.example.com", description="URL the winning deploy publishes")

    def deploy(
        self,
        script_path: Path,
        *,
        app_name: str,
        environment_name: str | None = None,
        extra_env: Mapping[str, str] = {},
    ) -> None:
        self.deploy_attempts = self.deploy_attempts + 1
        if self.deploy_attempts == 1:
            _publish(self, app_name, self.winner_url)
            raise ModalProxyAppLockedError("The selected app is locked")
        super().deploy(script_path, app_name=app_name, environment_name=environment_name, extra_env=extra_env)


class _PermanentlyAppLockedInterface(FakeModalInterface):
    """Refuses every deploy as app-locked, and never publishes anything."""

    deploy_attempts: int = Field(default=0, description="How many deploys this interface was asked to perform")

    def deploy(
        self,
        script_path: Path,
        *,
        app_name: str,
        environment_name: str | None = None,
        extra_env: Mapping[str, str] = {},
    ) -> None:
        self.deploy_attempts = self.deploy_attempts + 1
        raise ModalProxyAppLockedError("The selected app is locked")


class _TransientlyFailingDeployInterface(FakeModalInterface):
    """Fails its first deploy with a Modal-side failure another attempt can clear, then succeeds.

    Stands in for Modal failing a deploy of its own accord, as opposed to
    reaching a verdict on the script being deployed.
    """

    deploy_attempts: int = Field(default=0, description="How many deploys this interface was asked to perform")
    failure_type: type[ModalProxyDeployRetryableError] = Field(
        description="The retryable failure the first deploy raises"
    )

    def deploy(
        self,
        script_path: Path,
        *,
        app_name: str,
        environment_name: str | None = None,
        extra_env: Mapping[str, str] = {},
    ) -> None:
        self.deploy_attempts = self.deploy_attempts + 1
        if self.deploy_attempts == 1:
            raise self.failure_type("Modal failed this deploy without judging it")
        super().deploy(script_path, app_name=app_name, environment_name=environment_name, extra_env=extra_env)


class _FailingDeployInterface(FakeModalInterface):
    """Fails every deploy for a reason another attempt cannot clear."""

    deploy_attempts: int = Field(default=0, description="How many deploys this interface was asked to perform")

    def deploy(
        self,
        script_path: Path,
        *,
        app_name: str,
        environment_name: str | None = None,
        extra_env: Mapping[str, str] = {},
    ) -> None:
        self.deploy_attempts = self.deploy_attempts + 1
        raise ModalProxyError("Image build failed")


def test_an_app_that_already_carries_this_source_is_not_deployed_to_again(
    tmp_path: Path, cg: ConcurrencyGroup
) -> None:
    interface = _PermanentlyAppLockedInterface(root_dir=tmp_path / "modal", concurrency_group=cg)
    _publish(interface, _APP, "https://already-there.example.com")

    url = ensure_function_deployed(_FUNCTION, _APP, _ENV, interface)

    assert url == "https://already-there.example.com"
    assert interface.deploy_attempts == 0


def test_losing_the_deploy_race_takes_the_winners_endpoint_instead_of_deploying_again(
    tmp_path: Path, cg: ConcurrencyGroup
) -> None:
    """A create refused the app lock must adopt the deploy that beat it to this source.

    Deploying anyway is wasted work that holds the lock against every create
    still queued behind it.
    """
    interface = _AppLockedUntilAnotherDeployWinsInterface(root_dir=tmp_path / "modal", concurrency_group=cg)

    url = ensure_function_deployed(_FUNCTION, _APP, _ENV, interface)

    assert url == "https://winner.example.com"
    assert interface.deploy_attempts == 1, "a create that lost the race must not deploy the same source again"


def test_a_deploy_refused_the_app_lock_without_a_winner_is_retried(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Contention with nothing published yet still has to deploy -- just not forever."""
    interface = _PermanentlyAppLockedInterface(root_dir=tmp_path / "modal", concurrency_group=cg)

    with pytest.raises(MngrError):
        ensure_function_deployed(_FUNCTION, _APP, _ENV, interface, retry_budget_seconds=0.2, max_backoff_seconds=0.05)

    assert interface.deploy_attempts > 1, "app-lock contention is transient and must be retried"


@pytest.mark.parametrize(
    "failure_type",
    [ModalProxyImageBuilderLostError, ModalProxyDeployTimeoutError],
)
def test_a_deploy_modal_failed_without_judging_it_is_retried(
    failure_type: type[ModalProxyDeployRetryableError], tmp_path: Path, cg: ConcurrencyGroup
) -> None:
    """Modal failing a deploy of its own accord is not a verdict on the script, so deploy it again."""
    interface = _TransientlyFailingDeployInterface(
        root_dir=tmp_path / "modal", concurrency_group=cg, failure_type=failure_type
    )

    url = ensure_function_deployed(_FUNCTION, _APP, _ENV, interface, max_backoff_seconds=0.05)

    assert url.endswith(f"/{_APP}/{_FUNCTION}")
    assert interface.deploy_attempts == 2


def test_a_deploy_that_fails_for_any_other_reason_is_not_retried(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    interface = _FailingDeployInterface(root_dir=tmp_path / "modal", concurrency_group=cg)

    with pytest.raises(MngrError):
        ensure_function_deployed(_FUNCTION, _APP, _ENV, interface)

    assert interface.deploy_attempts == 1


class _AppPresenceAtDeployInterface(FakeModalInterface):
    """Counts deploys arriving before their app exists."""

    deploy_count_with_app_absent: int = Field(
        default=0, description="Deploys that arrived while the target app did not exist"
    )

    def deploy(
        self,
        script_path: Path,
        *,
        app_name: str,
        environment_name: str | None = None,
        extra_env: Mapping[str, str] = {},
    ) -> None:
        if environment_name is None:
            raise ModalProxyInvalidError("Recording app presence needs a named environment to look the app up in")
        try:
            self.app_lookup(app_name, create_if_missing=False, environment_name=environment_name)
        except ModalProxyNotFoundError:
            self.deploy_count_with_app_absent = self.deploy_count_with_app_absent + 1
        super().deploy(script_path, app_name=app_name, environment_name=environment_name, extra_env=extra_env)


def test_a_deploy_is_never_what_brings_its_app_into_existence(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Concurrent first deploys of one app name knock each other over."""
    interface = _AppPresenceAtDeployInterface(root_dir=tmp_path / "modal", concurrency_group=cg)

    url = ensure_function_deployed(_FUNCTION, _APP, _ENV, interface)

    assert _FUNCTION in url
    assert len(interface.get_deployments()) == 1
    assert interface.deploy_count_with_app_absent == 0


def test_the_route_script_creates_the_state_volume_at_the_version_the_backend_opens() -> None:
    """Whichever of the deploy and the backend creates the volume first, the other must be able to open it.

    The route script is standalone and cannot import the constant, so it
    restates it.
    """
    tree = ast.parse(get_route_script_path(_FUNCTION).read_text())
    volume_from_name_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "from_name"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "Volume"
    ]
    assert len(volume_from_name_calls) == 1
    requested_versions = [keyword.value for keyword in volume_from_name_calls[0].keywords if keyword.arg == "version"]
    assert len(requested_versions) == 1, "the route script must say which version it creates the volume at"
    assert isinstance(requested_versions[0], ast.Constant)
    assert requested_versions[0].value == STATE_VOLUME_FS_VERSION
