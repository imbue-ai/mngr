import pytest

from imbue.mngr.cli.backend_hosts import provider_names_on_backend
from imbue.mngr.cli.backend_hosts import resolve_host_on_backend
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.config import LocalProviderConfig
from imbue.mngr.providers.ssh.backend import SSH_BACKEND_NAME
from imbue.mngr.providers.ssh.config import SSHProviderConfig


def test_provider_names_on_backend_keep_only_that_backends_instances(
    temp_mngr_ctx: MngrContext, mngr_test_prefix: str
) -> None:
    config = MngrConfig(
        default_host_dir=temp_mngr_ctx.config.default_host_dir,
        prefix=mngr_test_prefix,
        providers={
            ProviderInstanceName("my-pool"): SSHProviderConfig(),
            ProviderInstanceName("my-local"): LocalProviderConfig(backend=ProviderBackendName("local")),
        },
    )
    mngr_ctx = MngrContext(config=config, pm=temp_mngr_ctx.pm, profile_dir=temp_mngr_ctx.profile_dir)

    assert provider_names_on_backend(mngr_ctx, SSH_BACKEND_NAME) == ("my-pool", "ssh")
    assert provider_names_on_backend(mngr_ctx, ProviderBackendName("local")) == ("my-local", "local")


def test_resolve_host_on_backend_refuses_to_scan_other_providers_when_none_is_enabled(
    temp_mngr_ctx: MngrContext, mngr_test_prefix: str
) -> None:
    # Restrict discovery to the local backend so no ssh instance (not even the default one) is enabled.
    config = MngrConfig(
        default_host_dir=temp_mngr_ctx.config.default_host_dir,
        prefix=mngr_test_prefix,
        enabled_backends=[ProviderBackendName("local")],
    )
    mngr_ctx = MngrContext(config=config, pm=temp_mngr_ctx.pm, profile_dir=temp_mngr_ctx.profile_dir)
    with pytest.raises(UserInputError, match="No ssh provider is enabled"):
        resolve_host_on_backend(HostAddress(host=HostName("my-host")), mngr_ctx, SSH_BACKEND_NAME)
