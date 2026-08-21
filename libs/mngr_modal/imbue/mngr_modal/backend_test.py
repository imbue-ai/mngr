from pathlib import Path
from typing import Final

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderEmptyError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import UserId
from imbue.mngr_modal.backend import ModalProviderBackend
from imbue.mngr_modal.config import ModalMode
from imbue.mngr_modal.config import ModalProviderConfig
from imbue.mngr_modal.plugin import get_files_for_deploy
from imbue.modal_proxy.direct import DirectModalInterface
from imbue.modal_proxy.errors import ModalProxyError
from imbue.modal_proxy.interface import AppInterface
from imbue.modal_proxy.interface import ModalInterface
from imbue.modal_proxy.testing import FakeModalInterface
from imbue.modal_proxy.testing import MODAL_UNREACHABLE_MESSAGE
from imbue.modal_proxy.testing import UnreachableModalInterface

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


def test_get_files_for_deploy_returns_empty_when_no_modal_dir(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy returns empty dict when no modal provider directory exists."""
    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=tmp_path
    )

    assert result == {}


def test_get_files_for_deploy_excludes_ssh_key_files(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy excludes SSH key files from the modal provider directory."""
    modal_dir = temp_mngr_ctx.profile_dir / "providers" / "modal"
    modal_dir.mkdir(parents=True)
    (modal_dir / "modal_ssh_key").write_text("private-key-data")
    (modal_dir / "modal_ssh_key.pub").write_text("public-key-data")
    (modal_dir / "known_hosts").write_text("[localhost]:2222 ssh-ed25519 AAAA...")

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=tmp_path
    )

    assert result == {}


def test_get_files_for_deploy_includes_non_key_files(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy includes non-key files from the modal provider directory."""
    modal_dir = temp_mngr_ctx.profile_dir / "providers" / "modal"
    modal_dir.mkdir(parents=True)
    config_file = modal_dir / "config.json"
    config_file.write_text('{"modal": "config"}')

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=tmp_path
    )

    assert len(result) == 1
    matched_values = list(result.values())
    assert matched_values[0] == config_file


# =============================================================================
# ModalMode resolution -- no Modal/network calls
# =============================================================================


def test_resolve_modal_interface_direct_uses_sdk() -> None:
    """DIRECT mode resolves to the SDK-backed interface."""
    iface = ModalProviderBackend._resolve_modal_interface(ModalProviderConfig())
    assert isinstance(iface, DirectModalInterface)


def test_resolve_modal_interface_proxied_is_not_implemented() -> None:
    """PROXIED is intentionally not implemented (Modal is imbue-internal; auth directly via DIRECT)."""
    config = ModalProviderConfig(mode=ModalMode.PROXIED)
    with pytest.raises(NotImplementedError):
        ModalProviderBackend._resolve_modal_interface(config)


# =============================================================================
# Construction against an unreachable Modal
# =============================================================================


# What a Modal that answered -- with a version mismatch, a serialization fault,
# or a bug of our own -- looks like once translated: a bare ModalProxyError,
# which is a real failure of the operation and not a reason to skip Modal.
_MODAL_ANSWERED_WITH_A_FAILURE: Final[str] = "unsupported client version"


class _BuggyModalInterface(FakeModalInterface):
    """A Modal that is perfectly reachable and fails anyway."""

    def app_lookup(
        self,
        name: str,
        *,
        create_if_missing: bool = True,
        environment_name: str,
    ) -> AppInterface:
        raise ModalProxyError(_MODAL_ANSWERED_WITH_A_FAILURE)


def _construct_against(
    modal_interface: ModalInterface,
    temp_mngr_ctx: MngrContext,
    is_persistent: bool = True,
    is_environment_creation_allowed: bool = False,
) -> ProviderInstanceInterface:
    """Run the real Modal construction body against the given interface."""
    return ModalProviderBackend._construct_modal_provider(
        ProviderInstanceName("modal"),
        ModalProviderConfig(user_id=UserId("test-user"), is_persistent=is_persistent),
        temp_mngr_ctx,
        modal_interface,
        is_environment_creation_allowed=is_environment_creation_allowed,
    )


# Persistent apps are looked up; ephemeral ones are created locally and then
# have their run context entered. Both cross the network, at different calls.
@pytest.mark.parametrize("is_persistent", [True, False], ids=["persistent_app", "ephemeral_app"])
def test_construction_reports_an_unreachable_modal_as_an_unavailable_provider(
    temp_mngr_ctx: MngrContext, tmp_path: Path, cg: ConcurrencyGroup, is_persistent: bool
) -> None:
    """A Modal we could not connect to leaves the provider unavailable, not the command broken.

    ``ProviderUnavailableError`` is the one construction failure (besides
    ``ProviderEmptyError``) that provider enumeration is willing to skip, so it
    is what keeps a laptop that lost its network from failing every mngr
    command -- including the ones that have nothing to do with Modal.
    """
    modal_interface = UnreachableModalInterface(root_dir=tmp_path / "modal", concurrency_group=cg)

    with pytest.raises(ProviderUnavailableError) as exc_info:
        _construct_against(modal_interface, temp_mngr_ctx, is_persistent=is_persistent)

    # Not "empty": that would assert Modal is known to hold nothing, which is
    # precisely what an unreachable backend cannot tell us.
    assert not isinstance(exc_info.value, ProviderEmptyError)
    # The SDK's own explanation has to survive, or nobody can tell a dropped
    # network apart from any other reason Modal went quiet.
    assert MODAL_UNREACHABLE_MESSAGE in str(exc_info.value)


def test_an_unreachable_modal_does_not_tell_the_user_to_start_docker(
    temp_mngr_ctx: MngrContext, tmp_path: Path, cg: ConcurrencyGroup
) -> None:
    """The remediation must be Modal's, not the base class's Docker default.

    ``ProviderUnavailableError`` defaults its help to "start Docker", which is
    useless-to-misleading advice for a cloud backend -- curated text is the
    reason its constructor takes the argument at all.
    """
    modal_interface = UnreachableModalInterface(root_dir=tmp_path / "modal", concurrency_group=cg)

    with pytest.raises(ProviderUnavailableError) as exc_info:
        _construct_against(modal_interface, temp_mngr_ctx)

    help_text = exc_info.value.user_help_text
    assert help_text is not None
    assert "Docker" not in help_text
    # `mngr list` renders one glanceable line per provider from short_reason, so
    # it has to be curated rather than defaulted to the reason -- which inlines
    # the SDK's whole sentence.
    assert MODAL_UNREACHABLE_MESSAGE not in exc_info.value.short_reason
    assert exc_info.value.short_remediation is not None


def test_a_modal_failure_that_is_not_a_connection_failure_stays_fatal(
    temp_mngr_ctx: MngrContext, tmp_path: Path, cg: ConcurrencyGroup
) -> None:
    """Only the connectivity case is reclassified; everything else still crashes.

    ``ModalProxyError`` also covers failures Modal answered with -- and outright
    bugs on our side. Calling those "unavailable" would make provider
    enumeration swallow them, so a broken Modal integration would silently
    present as an empty listing instead of an error anyone would fix.
    """
    modal_interface = _BuggyModalInterface(root_dir=tmp_path / "modal", concurrency_group=cg)

    with pytest.raises(MngrError) as exc_info:
        _construct_against(modal_interface, temp_mngr_ctx)

    assert not isinstance(exc_info.value, ProviderUnavailableError)


def test_bootstrapping_for_host_creation_surfaces_an_unreachable_modal(
    temp_mngr_ctx: MngrContext, tmp_path: Path, cg: ConcurrencyGroup
) -> None:
    """``mngr create @.modal`` must fail loudly rather than skip the provider.

    Creating a host on Modal is a targeted request: silently proceeding without
    Modal is not a thing the user could have meant. The create path calls this
    directly and does not tolerate the error, so raising it here is what makes
    the failure legible instead of a later "provider not found".
    """
    modal_interface = UnreachableModalInterface(root_dir=tmp_path / "modal", concurrency_group=cg)

    with pytest.raises(ProviderUnavailableError) as exc_info:
        _construct_against(modal_interface, temp_mngr_ctx, is_environment_creation_allowed=True)

    assert MODAL_UNREACHABLE_MESSAGE in str(exc_info.value)
