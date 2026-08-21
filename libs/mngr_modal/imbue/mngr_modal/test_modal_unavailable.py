"""How the rest of mngr behaves while the Modal control plane is unreachable.

The unit coverage in ``backend_test.py`` pins the error class the Modal backend
raises. These tests pin what that class buys: a laptop whose network dropped
still gets answers about the providers it *can* reach, and an agent lookup that
comes up empty blames Modal rather than the agent.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import find_all_agents
from imbue.mngr.api.providers import get_all_provider_instances_and_skipped
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.config.provider_config_registry import _provider_config_registry
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.errors import parse_provider_unavailable_reason
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.registry import _backend_registry
from imbue.mngr_modal.backend import ModalProviderBackend
from imbue.mngr_modal.config import ModalProviderConfig
from imbue.mngr_modal.plugin import MODAL_BUILD_ARGS_HELP
from imbue.mngr_modal.plugin import MODAL_START_ARGS_HELP
from imbue.modal_proxy.interface import ModalInterface
from imbue.modal_proxy.testing import MODAL_UNREACHABLE_MESSAGE
from imbue.modal_proxy.testing import UnreachableModalInterface

# The Modal plugin is not registered in tests (the shared plugin manager loads
# local + ssh only), so the backend under test is registered under a name of its
# own rather than shadowing "modal".
_UNREACHABLE_MODAL_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("test-unreachable-modal")
_UNREACHABLE_MODAL_PROVIDER_NAME: Final[ProviderInstanceName] = ProviderInstanceName(
    str(_UNREACHABLE_MODAL_BACKEND_NAME)
)


@contextmanager
def _registered_unreachable_modal_backend(modal_interface: ModalInterface) -> Iterator[None]:
    """Register the real Modal backend body, wired to a Modal that cannot be reached.

    Only the Modal SDK is stood in for. The construction body -- and therefore
    the error class it picks when the control plane does not answer -- is the
    production one, which is the whole point: these tests must fail if that
    choice regresses.
    """

    class _UnreachableModalBackend(ProviderBackendInterface):
        """The Modal backend, pointed at a Modal whose control plane is unreachable."""

        @staticmethod
        def get_name() -> ProviderBackendName:
            return _UNREACHABLE_MODAL_BACKEND_NAME

        @staticmethod
        def get_description() -> str:
            return "The Modal backend, against a Modal that cannot be reached"

        @staticmethod
        def get_config_class() -> type[ProviderInstanceConfig]:
            return ModalProviderConfig

        @staticmethod
        def get_build_args_help() -> str:
            return MODAL_BUILD_ARGS_HELP

        @staticmethod
        def get_start_args_help() -> str:
            return MODAL_START_ARGS_HELP

        @staticmethod
        def build_provider_instance(
            name: ProviderInstanceName,
            config: ProviderInstanceConfig,
            mngr_ctx: MngrContext,
        ) -> ProviderInstanceInterface:
            return ModalProviderBackend._construct_modal_provider(name, config, mngr_ctx, modal_interface)

    _backend_registry[_UNREACHABLE_MODAL_BACKEND_NAME] = _UnreachableModalBackend
    _provider_config_registry[_UNREACHABLE_MODAL_BACKEND_NAME] = ModalProviderConfig
    try:
        yield
    finally:
        del _backend_registry[_UNREACHABLE_MODAL_BACKEND_NAME]
        del _provider_config_registry[_UNREACHABLE_MODAL_BACKEND_NAME]


@pytest.fixture
def unreachable_modal(tmp_path: Path, cg: ConcurrencyGroup) -> UnreachableModalInterface:
    return UnreachableModalInterface(root_dir=tmp_path / "unreachable_modal", concurrency_group=cg)


def test_provider_enumeration_keeps_going_when_modal_cannot_be_reached(
    temp_mngr_ctx: MngrContext, unreachable_modal: UnreachableModalInterface
) -> None:
    """Losing Modal must cost the user Modal, not every provider they have.

    This is the failure that was observed in the wild: with the network down,
    a ``mngr start`` for a *docker* workspace died on Modal before it ever got
    to Docker. Enumeration tolerates exactly two construction failures, so the
    class Modal raises decides whether one unreachable cloud backend takes the
    whole command with it.
    """
    with _registered_unreachable_modal_backend(unreachable_modal):
        providers, skipped = get_all_provider_instances_and_skipped(temp_mngr_ctx)

    assert [str(entry.provider_name) for entry in skipped] == [str(_UNREACHABLE_MODAL_PROVIDER_NAME)]
    # Skipped, but not written off as empty: nothing was reached, so what Modal
    # holds is unknown and callers must keep treating it as a gap.
    assert skipped[0].is_empty is False
    assert MODAL_UNREACHABLE_MESSAGE in skipped[0].error_message
    # The providers that *are* reachable still came through, which is the point.
    assert LOCAL_PROVIDER_NAME in [provider.name for provider in providers]


def test_discovery_still_answers_for_the_other_providers(
    temp_mngr_ctx: MngrContext, unreachable_modal: UnreachableModalInterface
) -> None:
    """A full discovery pass completes and records Modal as the gap it is."""
    with _registered_unreachable_modal_backend(unreachable_modal):
        outcome = discover_hosts_and_agents(
            temp_mngr_ctx,
            provider_names=None,
            agent_identifiers=None,
            include_destroyed=False,
            reset_caches=False,
        )

    assert [str(entry.provider_name) for entry in outcome.unavailable_providers] == [
        str(_UNREACHABLE_MODAL_PROVIDER_NAME)
    ]
    assert LOCAL_PROVIDER_NAME in [provider.name for provider in outcome.providers]


def test_an_agent_lookup_blames_the_unreachable_modal_rather_than_the_agent(
    temp_mngr_ctx: MngrContext, unreachable_modal: UnreachableModalInterface
) -> None:
    """Skipping Modal must not turn a Modal-hosted agent into "no such agent".

    Being skippable is only safe because the lookup path re-raises for whatever
    it could not see: with Modal unreachable, whether the agent lives there is
    exactly what nobody can establish, so the outage is the honest answer and
    the one that tells the reader what to do about it.
    """
    agent_name = "some-modal-agent"

    with _registered_unreachable_modal_backend(unreachable_modal):
        with pytest.raises(ProviderUnavailableError) as exc_info:
            find_all_agents(
                addresses=[AgentAddress(agent=AgentName(agent_name))],
                filter_all=False,
                target_state=None,
                mngr_ctx=temp_mngr_ctx,
            )

    message = str(exc_info.value)
    assert MODAL_UNREACHABLE_MESSAGE in message
    assert agent_name in message
    # Callers out of process (minds' recovery card) recover the reason by
    # parsing this, and only a message naming *their* provider answers.
    assert parse_provider_unavailable_reason(message, str(_UNREACHABLE_MODAL_PROVIDER_NAME)) is not None
    # Modal's own remediation survives the round trip through the skip, instead
    # of being replaced by the base class's "start Docker".
    help_text = exc_info.value.user_help_text
    assert help_text is not None
    assert "Docker" not in help_text
