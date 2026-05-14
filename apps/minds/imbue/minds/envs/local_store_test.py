import stat
from pathlib import Path

import pytest
from pydantic import AnyUrl
from pydantic import SecretStr

from imbue.minds.config.loader import load_client_config
from imbue.minds.envs.local_store import LocalDevEnvConfig
from imbue.minds.envs.local_store import delete_dev_env_file
from imbue.minds.envs.local_store import list_dev_env_files
from imbue.minds.envs.local_store import read_dev_env_file
from imbue.minds.envs.local_store import write_dev_env_file
from imbue.minds.envs.primitives import DevEnvAlreadyExistsError
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.primitives import DevEnvNotFoundError
from imbue.minds.envs.primitives import InvalidDevEnvNameError


@pytest.fixture
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect ``Path.home()`` to ``tmp_path`` so writes land under tmp."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MINDS_ROOT_NAME", "tname")
    return tmp_path


def _make_config() -> LocalDevEnvConfig:
    return LocalDevEnvConfig(
        connector_url=AnyUrl("https://test-connector.modal.run"),
        litellm_proxy_url=AnyUrl("https://test-litellm.modal.run"),
        secrets={"NEON_DSN": SecretStr("postgres://example")},
    )


def test_round_trip_write_read(_isolated_home: Path) -> None:
    write_dev_env_file(_make_config(), name=DevEnvName("alice"))
    loaded = read_dev_env_file(DevEnvName("alice"))
    assert str(loaded.connector_url) == "https://test-connector.modal.run/"
    assert loaded.secrets["NEON_DSN"].get_secret_value() == "postgres://example"


def test_written_file_is_loadable_as_client_config(_isolated_home: Path) -> None:
    """The per-dev-env TOML must be consumable by `minds run --config-file <path>`.

    Regression test: the previous shape nested the URLs under [client] and
    added a top-level ``name`` field, which load_client_config rejected
    because ClientEnvConfig requires connector_url / litellm_proxy_url at
    the top level.
    """
    target = write_dev_env_file(_make_config(), name=DevEnvName("frank"))
    loaded = load_client_config(target)
    assert str(loaded.connector_url) == "https://test-connector.modal.run/"
    assert str(loaded.litellm_proxy_url) == "https://test-litellm.modal.run/"


def test_write_sets_mode_600(_isolated_home: Path) -> None:
    target = write_dev_env_file(_make_config(), name=DevEnvName("bob"))
    file_mode = target.stat().st_mode & 0o777
    # No group or other bits should be set.
    assert file_mode == stat.S_IRUSR | stat.S_IWUSR, oct(file_mode)


def test_write_refuses_to_clobber_existing(_isolated_home: Path) -> None:
    write_dev_env_file(_make_config(), name=DevEnvName("carol"))
    with pytest.raises(DevEnvAlreadyExistsError, match="already exists"):
        write_dev_env_file(_make_config(), name=DevEnvName("carol"))


def test_write_overwrite_true_replaces_existing(_isolated_home: Path) -> None:
    write_dev_env_file(_make_config(), name=DevEnvName("dan"))
    new_config = LocalDevEnvConfig(
        connector_url=AnyUrl("https://changed.modal.run"),
        litellm_proxy_url=AnyUrl("https://changed-litellm.modal.run"),
        secrets={},
    )
    write_dev_env_file(new_config, name=DevEnvName("dan"), overwrite=True)
    loaded = read_dev_env_file(DevEnvName("dan"))
    assert str(loaded.connector_url) == "https://changed.modal.run/"
    assert loaded.secrets == {}


def test_read_missing_raises(_isolated_home: Path) -> None:
    with pytest.raises(DevEnvNotFoundError, match="No dev env file found"):
        read_dev_env_file(DevEnvName("nobody"))


def test_delete_returns_false_for_missing(_isolated_home: Path) -> None:
    assert delete_dev_env_file(DevEnvName("ghost")) is False


def test_delete_returns_true_when_removed(_isolated_home: Path) -> None:
    write_dev_env_file(_make_config(), name=DevEnvName("eve"))
    assert delete_dev_env_file(DevEnvName("eve")) is True
    with pytest.raises(DevEnvNotFoundError):
        read_dev_env_file(DevEnvName("eve"))


def test_list_dev_env_files_returns_sorted(_isolated_home: Path) -> None:
    write_dev_env_file(_make_config(), name=DevEnvName("zeta"))
    write_dev_env_file(_make_config(), name=DevEnvName("alpha"))
    write_dev_env_file(_make_config(), name=DevEnvName("mu"))
    files = list_dev_env_files()
    assert [p.stem for p in files] == ["alpha", "mu", "zeta"]


def test_invalid_dev_env_name_raises() -> None:
    with pytest.raises(InvalidDevEnvNameError):
        DevEnvName("UPPERCASE-NOT-OK")
    # Single character is below the 2-char minimum in the pattern.
    with pytest.raises(InvalidDevEnvNameError):
        DevEnvName("a")
    with pytest.raises(InvalidDevEnvNameError):
        DevEnvName("-leading-hyphen")
