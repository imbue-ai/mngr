"""Unit tests for the imbue_cloud plugin's on_load_config hook.

The hook silently disables the default ``[providers.imbue_cloud]`` instance
whenever no accounts are signed in, which keeps subprocess invocations of
``mngr list`` (e.g. from Modal acceptance tests) from failing on a missing
account. These tests cover the matrix of (configured?, signed in?,
default_host_dir state).
"""

from pathlib import Path
from typing import Any

from pydantic import SecretStr

from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_imbue_cloud.config import ImbueCloudProviderConfig
from imbue.mngr_imbue_cloud.config import get_sessions_dir
from imbue.mngr_imbue_cloud.data_types import AuthSession
from imbue.mngr_imbue_cloud.plugin import on_load_config
from imbue.mngr_imbue_cloud.primitives import IMBUE_CLOUD_BACKEND_NAME
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.primitives import SuperTokensUserId
from imbue.mngr_imbue_cloud.session_store import ImbueCloudSessionStore


def _make_session(email: str = "alice@imbue.com", user_id: str = "user-abc") -> AuthSession:
    return AuthSession(
        user_id=SuperTokensUserId(user_id),
        email=ImbueCloudAccount(email),
        display_name=None,
        access_token=SecretStr("header.payload.sig"),
        refresh_token=SecretStr("refresh-tok"),
        access_token_expires_at=None,
    )


def _initialize_host_dir(host_dir: Path, profile_id: str = "abc123") -> Path:
    """Write a minimal ``<host_dir>/config.toml`` and return the profile dir.

    Mirrors what ``get_or_create_profile_dir`` does so the plugin's
    ``_resolve_profile_dir`` can find the profile.
    """
    host_dir.mkdir(parents=True, exist_ok=True)
    (host_dir / "config.toml").write_text(f'profile = "{profile_id}"\n')
    profile_dir = host_dir / "profiles" / profile_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def test_on_load_config_disables_default_when_no_accounts(tmp_path: Path) -> None:
    """When no accounts are signed in, the hook injects is_enabled=False."""
    _initialize_host_dir(tmp_path)
    config_dict: dict[str, Any] = {
        "default_host_dir": tmp_path,
        "providers": {},
    }

    on_load_config(config_dict)

    providers = config_dict["providers"]
    default_name = ProviderInstanceName(IMBUE_CLOUD_BACKEND_NAME)
    assert default_name in providers
    injected = providers[default_name]
    assert isinstance(injected, ImbueCloudProviderConfig)
    assert injected.is_enabled is False
    assert str(injected.backend) == IMBUE_CLOUD_BACKEND_NAME


def test_on_load_config_leaves_explicit_provider_alone(tmp_path: Path) -> None:
    """If the user has explicitly configured [providers.imbue_cloud], do nothing."""
    _initialize_host_dir(tmp_path)
    default_name = ProviderInstanceName(IMBUE_CLOUD_BACKEND_NAME)
    user_provider = ImbueCloudProviderConfig(
        backend=ProviderBackendName(IMBUE_CLOUD_BACKEND_NAME),
        is_enabled=True,
    )
    config_dict: dict[str, Any] = {
        "default_host_dir": tmp_path,
        "providers": {default_name: user_provider},
    }

    on_load_config(config_dict)

    # User's explicit config is preserved exactly.
    assert config_dict["providers"][default_name] is user_provider
    assert config_dict["providers"][default_name].is_enabled is True


def test_on_load_config_does_not_inject_when_accounts_exist(tmp_path: Path) -> None:
    """When at least one account is signed in, the hook is a no-op."""
    profile_dir = _initialize_host_dir(tmp_path)
    store = ImbueCloudSessionStore(sessions_dir=get_sessions_dir(profile_dir))
    store.save(_make_session())

    config_dict: dict[str, Any] = {
        "default_host_dir": tmp_path,
        "providers": {},
    }

    on_load_config(config_dict)

    default_name = ProviderInstanceName(IMBUE_CLOUD_BACKEND_NAME)
    assert default_name not in config_dict["providers"]


def test_on_load_config_injects_when_default_host_dir_missing() -> None:
    """If ``default_host_dir`` is absent from config_dict, treat as no accounts."""
    config_dict: dict[str, Any] = {"providers": {}}

    on_load_config(config_dict)

    default_name = ProviderInstanceName(IMBUE_CLOUD_BACKEND_NAME)
    assert default_name in config_dict["providers"]
    assert config_dict["providers"][default_name].is_enabled is False


def test_on_load_config_injects_when_host_dir_has_no_config_toml(tmp_path: Path) -> None:
    """An uninitialized host_dir (no config.toml) counts as no accounts."""
    # Note: tmp_path exists but has no config.toml.
    config_dict: dict[str, Any] = {
        "default_host_dir": tmp_path,
        "providers": {},
    }

    on_load_config(config_dict)

    default_name = ProviderInstanceName(IMBUE_CLOUD_BACKEND_NAME)
    assert default_name in config_dict["providers"]
    assert config_dict["providers"][default_name].is_enabled is False


def test_on_load_config_injects_when_config_toml_has_no_profile(tmp_path: Path) -> None:
    """A config.toml without a ``profile`` field also counts as no accounts."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.toml").write_text("# no profile field\n")
    config_dict: dict[str, Any] = {
        "default_host_dir": tmp_path,
        "providers": {},
    }

    on_load_config(config_dict)

    default_name = ProviderInstanceName(IMBUE_CLOUD_BACKEND_NAME)
    assert default_name in config_dict["providers"]
    assert config_dict["providers"][default_name].is_enabled is False


def test_on_load_config_injects_when_config_toml_is_malformed(tmp_path: Path) -> None:
    """A malformed config.toml is treated as no accounts (with a warning)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.toml").write_text("this is = not valid toml = at all\n[")
    config_dict: dict[str, Any] = {
        "default_host_dir": tmp_path,
        "providers": {},
    }

    on_load_config(config_dict)

    default_name = ProviderInstanceName(IMBUE_CLOUD_BACKEND_NAME)
    assert default_name in config_dict["providers"]
    assert config_dict["providers"][default_name].is_enabled is False


def test_on_load_config_does_not_disable_named_account_provider(tmp_path: Path) -> None:
    """Per-account instances like ``[providers.imbue_cloud_alice]`` are untouched.

    The hook keys on the bare backend name only; named instances should be
    preserved regardless of session state.
    """
    _initialize_host_dir(tmp_path)
    named_provider_name = ProviderInstanceName("imbue_cloud_alice")
    named_provider = ImbueCloudProviderConfig(
        backend=ProviderBackendName(IMBUE_CLOUD_BACKEND_NAME),
        account=ImbueCloudAccount("alice@imbue.com"),
        is_enabled=True,
    )
    providers: dict[ProviderInstanceName, ProviderInstanceConfig] = {named_provider_name: named_provider}
    config_dict: dict[str, Any] = {
        "default_host_dir": tmp_path,
        "providers": providers,
    }

    on_load_config(config_dict)

    # The named provider remains unchanged.
    assert config_dict["providers"][named_provider_name] is named_provider
    # And because no accounts are signed in, the bare default is still injected.
    default_name = ProviderInstanceName(IMBUE_CLOUD_BACKEND_NAME)
    assert default_name in config_dict["providers"]
    assert config_dict["providers"][default_name].is_enabled is False
