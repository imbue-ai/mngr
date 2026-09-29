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
from imbue.mngr.providers.docker.backend import DOCKER_BACKEND_NAME
from imbue.mngr.providers.docker.config import DockerProviderConfig
from imbue.mngr.providers.local.config import LocalProviderConfig


def test_provider_names_on_backend_keep_only_that_backends_instances(
    temp_mngr_ctx: MngrContext, mngr_test_prefix: str
) -> None:
    config = MngrConfig(
        default_host_dir=temp_mngr_ctx.config.default_host_dir,
        prefix=mngr_test_prefix,
        providers={
            ProviderInstanceName("remote-docker"): DockerProviderConfig(isolate_host_volumes=False),
            ProviderInstanceName("my-local"): LocalProviderConfig(backend=ProviderBackendName("local")),
        },
    )
    mngr_ctx = MngrContext(config=config, pm=temp_mngr_ctx.pm, profile_dir=temp_mngr_ctx.profile_dir)

    # The test plugin manager registers only the local backend, so there is no default docker instance to list.
    assert provider_names_on_backend(mngr_ctx, DOCKER_BACKEND_NAME) == ("remote-docker",)
    assert provider_names_on_backend(mngr_ctx, ProviderBackendName("local")) == ("my-local", "local")


def test_resolve_host_on_backend_refuses_to_scan_other_providers_when_none_is_enabled(
    temp_mngr_ctx: MngrContext,
) -> None:
    with pytest.raises(UserInputError, match="No docker provider is enabled"):
        resolve_host_on_backend(HostAddress(host=HostName("my-host")), temp_mngr_ctx, DOCKER_BACKEND_NAME)
