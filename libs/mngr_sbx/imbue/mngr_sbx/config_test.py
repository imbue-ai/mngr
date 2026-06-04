from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_sbx.config import SbxProviderConfig
from imbue.mngr_sbx.constants import DEFAULT_SBX_AGENT_TYPE
from imbue.mngr_sbx.constants import SBX_BACKEND_NAME


def test_sbx_provider_config_defaults_to_sbx_backend() -> None:
    config = SbxProviderConfig()
    assert config.backend == SBX_BACKEND_NAME
    assert config.backend == ProviderBackendName("sbx")


def test_sbx_provider_config_default_agent_type_is_docker_agent() -> None:
    config = SbxProviderConfig()
    assert config.default_agent_type == DEFAULT_SBX_AGENT_TYPE
    assert config.default_agent_type == "docker-agent"


def test_sbx_provider_config_default_template_is_none() -> None:
    config = SbxProviderConfig()
    assert config.default_template is None


def test_sbx_provider_config_accepts_overrides() -> None:
    config = SbxProviderConfig(
        default_agent_type="claude",
        default_template="my-image:latest",
        default_cpus=4,
        default_memory="8g",
    )
    assert config.default_agent_type == "claude"
    assert config.default_template == "my-image:latest"
    assert config.default_cpus == 4
    assert config.default_memory == "8g"
