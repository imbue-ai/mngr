from imbue.mngr.remediations import format_config_set
from imbue.mngr.remediations import format_config_unset
from imbue.mngr.remediations import format_disable_provider


def test_format_config_set_without_scope_omits_flag() -> None:
    assert format_config_set("providers.gcp.project_id", "<id>") == "mngr config set providers.gcp.project_id <id>"


def test_format_config_set_renders_scope_immediately_after_set() -> None:
    # The scope flag is the canonical position: right after ``set``, before the key.
    assert (
        format_config_set("agent_types.claude.isolate_local_config_dir", "false", scope="user")
        == "mngr config set --scope user agent_types.claude.isolate_local_config_dir false"
    )


def test_format_config_unset_without_scope_omits_flag() -> None:
    assert (
        format_config_unset("providers.azure.subscription_id") == "mngr config unset providers.azure.subscription_id"
    )


def test_format_config_unset_renders_scope_immediately_after_unset() -> None:
    assert format_config_unset("enabled_backends", scope="local") == "mngr config unset --scope local enabled_backends"


def test_format_disable_provider_recommends_local_scope() -> None:
    # Local is the highest-precedence scope, so the disable always takes effect
    # regardless of which layer currently enables the provider.
    assert format_disable_provider("azure") == "mngr config set --scope local providers.azure.is_enabled false"
