import stat
import tomllib
from pathlib import Path

import pytest
from pydantic import AnyUrl
from pydantic import SecretStr
from pydantic import ValidationError

from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.loader import EnvConfigError
from imbue.minds.config.loader import load_client_config
from imbue.minds.envs.local_store import DevEnvSecretsModel
from imbue.minds.envs.local_store import client_config_exists
from imbue.minds.envs.local_store import delete_env_root
from imbue.minds.envs.local_store import env_root_exists
from imbue.minds.envs.local_store import read_client_config_file
from imbue.minds.envs.local_store import read_secrets_file
from imbue.minds.envs.local_store import write_client_config
from imbue.minds.envs.local_store import write_secrets_file
from imbue.minds.envs.paths import client_config_file
from imbue.minds.envs.paths import env_root_dir
from imbue.minds.envs.paths import secrets_file
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.primitives import DevEnvNotFoundError
from imbue.minds.envs.primitives import InvalidDevEnvNameError


@pytest.fixture
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect ``Path.home()`` to ``tmp_path`` so writes land under tmp."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # No MINDS_ROOT_NAME -- the dev-env-name path computations don't need it
    # (env_root_dir derives the path purely from the DevEnvName).
    monkeypatch.delenv("MINDS_ROOT_NAME", raising=False)
    return tmp_path


def _make_client() -> ClientEnvConfig:
    return ClientEnvConfig(
        connector_url=AnyUrl("https://test-connector.modal.run"),
        litellm_proxy_url=AnyUrl("https://test-litellm.modal.run"),
    )


def _make_secrets() -> dict[str, SecretStr]:
    return {"NEON_DSN": SecretStr("postgres://example")}


def test_write_client_config_round_trip(_isolated_home: Path) -> None:
    target = write_client_config(_make_client(), name=DevEnvName("dev-alice"))
    assert target == client_config_file(DevEnvName("dev-alice"))
    loaded = read_client_config_file(DevEnvName("dev-alice"))
    assert str(loaded.connector_url) == "https://test-connector.modal.run/"
    assert str(loaded.litellm_proxy_url) == "https://test-litellm.modal.run/"


def test_write_client_config_is_loadable_as_client_config(_isolated_home: Path) -> None:
    """The per-dev-env client.toml must be consumable by `minds run --config-file <path>`.

    Regression check: dev / staging / production client.toml files all
    share the same shape so a dev env's TOML is shape-identical to a
    committed staging/production one.
    """
    target = write_client_config(_make_client(), name=DevEnvName("dev-frank"))
    loaded = load_client_config(target)
    assert str(loaded.connector_url) == "https://test-connector.modal.run/"
    assert str(loaded.litellm_proxy_url) == "https://test-litellm.modal.run/"


def test_write_client_config_has_no_secrets_subtable(_isolated_home: Path) -> None:
    """No secrets can land in client.toml -- the split-files invariant.

    The model has no ``secrets`` field, so even if a future caller tries
    to smuggle one through, ClientEnvConfig.model_validate rejects the
    file with ``extra="forbid"``.
    """
    target = write_client_config(_make_client(), name=DevEnvName("dev-alice"))
    raw = tomllib.loads(target.read_text())
    assert set(raw.keys()) == {"connector_url", "litellm_proxy_url"}
    # And the model itself refuses to accept a secrets key on load --
    # `extra="forbid"` surfaces as an EnvConfigError from load_client_config.
    target.write_text(target.read_text() + '\n[secrets]\nFOO = "bar"\n')
    with pytest.raises(EnvConfigError, match="Invalid client config"):
        read_client_config_file(DevEnvName("dev-alice"))


def test_write_client_config_mode_is_0644(_isolated_home: Path) -> None:
    target = write_client_config(_make_client(), name=DevEnvName("dev-bob"))
    mode = target.stat().st_mode & 0o777
    assert mode == 0o644, oct(mode)


def test_write_client_config_overwrites_existing(_isolated_home: Path) -> None:
    write_client_config(_make_client(), name=DevEnvName("dev-dan"))
    new = ClientEnvConfig(
        connector_url=AnyUrl("https://changed.modal.run"),
        litellm_proxy_url=AnyUrl("https://changed-litellm.modal.run"),
    )
    write_client_config(new, name=DevEnvName("dev-dan"))
    loaded = read_client_config_file(DevEnvName("dev-dan"))
    assert str(loaded.connector_url) == "https://changed.modal.run/"


def test_write_secrets_file_round_trip(_isolated_home: Path) -> None:
    target = write_secrets_file(_make_secrets(), name=DevEnvName("dev-alice"))
    assert target == secrets_file(DevEnvName("dev-alice"))
    loaded = read_secrets_file(DevEnvName("dev-alice"))
    assert isinstance(loaded, DevEnvSecretsModel)
    assert loaded.secrets["NEON_DSN"].get_secret_value() == "postgres://example"


def test_write_secrets_file_mode_is_0600(_isolated_home: Path) -> None:
    """Secrets file must be operator-only-readable -- chmod 600."""
    target = write_secrets_file(_make_secrets(), name=DevEnvName("dev-bob"))
    mode = target.stat().st_mode & 0o777
    assert mode == stat.S_IRUSR | stat.S_IWUSR, oct(mode)


def test_write_secrets_file_empty_is_still_a_file(_isolated_home: Path) -> None:
    """An empty secrets dict still produces a parseable file."""
    target = write_secrets_file({}, name=DevEnvName("dev-emi"))
    assert target.is_file()
    loaded = read_secrets_file(DevEnvName("dev-emi"))
    assert loaded.secrets == {}


def test_read_client_config_missing_raises(_isolated_home: Path) -> None:
    with pytest.raises(DevEnvNotFoundError, match="No client.toml"):
        read_client_config_file(DevEnvName("dev-nobody"))


def test_read_secrets_missing_returns_empty(_isolated_home: Path) -> None:
    """secrets.toml is optional for reads -- missing file means no secrets."""
    loaded = read_secrets_file(DevEnvName("dev-nobody"))
    assert loaded.secrets == {}


def test_env_root_exists(_isolated_home: Path) -> None:
    assert env_root_exists(DevEnvName("dev-ghost")) is False
    write_client_config(_make_client(), name=DevEnvName("dev-ghost"))
    assert env_root_exists(DevEnvName("dev-ghost")) is True


def test_client_config_exists(_isolated_home: Path) -> None:
    assert client_config_exists(DevEnvName("dev-ghost")) is False
    write_client_config(_make_client(), name=DevEnvName("dev-ghost"))
    assert client_config_exists(DevEnvName("dev-ghost")) is True


def test_delete_env_root(_isolated_home: Path) -> None:
    write_client_config(_make_client(), name=DevEnvName("dev-eve"))
    write_secrets_file(_make_secrets(), name=DevEnvName("dev-eve"))
    target = env_root_dir(DevEnvName("dev-eve"))
    assert target.is_dir()
    assert delete_env_root(DevEnvName("dev-eve")) is True
    assert not target.exists()


def test_delete_env_root_missing_returns_false(_isolated_home: Path) -> None:
    assert delete_env_root(DevEnvName("dev-ghost")) is False


def test_invalid_dev_env_name_raises() -> None:
    with pytest.raises(InvalidDevEnvNameError):
        DevEnvName("UPPERCASE-NOT-OK")
    # Single character is below the 2-char minimum in the pattern.
    with pytest.raises(InvalidDevEnvNameError):
        DevEnvName("a")
    with pytest.raises(InvalidDevEnvNameError):
        DevEnvName("-leading-hyphen")


def test_dev_env_secrets_model_rejects_extra_keys() -> None:
    """Round-trip safety -- the model forbids any non-secrets top-level field.

    Pass the extras through ``model_validate`` (with a dict literal)
    rather than the constructor so the call typechecks cleanly while
    still exercising pydantic's ``extra="forbid"`` rejection.
    """
    with pytest.raises(ValidationError):
        DevEnvSecretsModel.model_validate({"secrets": {}, "extra_key": "oops"})
