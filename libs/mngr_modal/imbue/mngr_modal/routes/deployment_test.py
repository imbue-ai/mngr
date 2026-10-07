from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.errors import MngrError
from imbue.mngr_modal.routes.deployment import ensure_function_deployed
from imbue.mngr_modal.routes.deployment import get_source_marker_function_name
from imbue.modal_proxy.errors import ModalProxyAppLockedError
from imbue.modal_proxy.errors import ModalProxyError
from imbue.modal_proxy.testing import FakeModalInterface

_FUNCTION = "snapshot_and_shutdown"
_APP = "mngr-test-app"


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


class _FailingDeployInterface(FakeModalInterface):
    """Fails every deploy for a reason that is not a concurrent modification."""

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

    url = ensure_function_deployed(_FUNCTION, _APP, None, interface)

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

    url = ensure_function_deployed(_FUNCTION, _APP, None, interface)

    assert url == "https://winner.example.com"
    assert interface.deploy_attempts == 1, "a create that lost the race must not deploy the same source again"


def test_a_deploy_refused_the_app_lock_without_a_winner_is_retried(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Contention with nothing published yet still has to deploy -- just not forever."""
    interface = _PermanentlyAppLockedInterface(root_dir=tmp_path / "modal", concurrency_group=cg)

    with pytest.raises(MngrError):
        ensure_function_deployed(
            _FUNCTION, _APP, None, interface, lock_retry_budget_seconds=0.2, max_backoff_seconds=0.05
        )

    assert interface.deploy_attempts > 1, "app-lock contention is transient and must be retried"


def test_a_deploy_that_fails_for_any_other_reason_is_not_retried(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    interface = _FailingDeployInterface(root_dir=tmp_path / "modal", concurrency_group=cg)

    with pytest.raises(MngrError):
        ensure_function_deployed(_FUNCTION, _APP, None, interface)

    assert interface.deploy_attempts == 1
