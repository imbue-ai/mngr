"""Project-level conftest for mng-claude.

When running tests from libs/mng_claude/, this conftest provides the common pytest hooks
that would otherwise come from the monorepo root conftest.py (which is not discovered
when pytest runs from a subdirectory).

When running from the monorepo root, the root conftest.py registers the hooks first,
and this file's register_conftest_hooks() call is a no-op (guarded by a module-level flag).
"""

import os
from pathlib import Path
from typing import Generator
from uuid import uuid4

import pytest
import toml

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mng.primitives import UserId
from imbue.mng.utils.logging import suppress_warnings
from imbue.mng.utils.testing import ModalSubprocessTestEnv
from imbue.mng.utils.testing import assert_home_is_temp_directory
from imbue.mng.utils.testing import delete_modal_apps_in_environment
from imbue.mng.utils.testing import delete_modal_environment
from imbue.mng.utils.testing import delete_modal_volumes_in_environment
from imbue.mng.utils.testing import generate_test_environment_name
from imbue.mng.utils.testing import get_subprocess_test_env
from imbue.mng.utils.testing import isolate_home

suppress_warnings()

register_marker("modal: marks tests that connect to the Modal cloud service")

register_conftest_hooks(globals())

pytest_plugins = ["imbue.mng.conftest"]


@pytest.fixture(autouse=True)
def setup_test_mng_env(
    tmp_home_dir: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    mng_test_root_name: str,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_tmux_server: None,
) -> Generator[None, None, None]:
    """Set up environment variables for all tests, including Modal tokens.

    This overrides mng's setup_test_mng_env to additionally load Modal
    credentials from ~/.modal.toml before HOME is overridden.
    """
    # Load modal token from real home before overriding HOME
    modal_toml_path = Path(os.path.expanduser("~/.modal.toml"))
    if modal_toml_path.exists():
        for value in toml.load(modal_toml_path).values():
            if value.get("active", ""):
                monkeypatch.setenv("MODAL_TOKEN_ID", value.get("token_id", ""))
                monkeypatch.setenv("MODAL_TOKEN_SECRET", value.get("token_secret", ""))
                break

    isolate_home(tmp_home_dir, monkeypatch)
    monkeypatch.setenv("MNG_HOST_DIR", str(temp_host_dir))
    monkeypatch.setenv("MNG_PREFIX", mng_test_prefix)
    monkeypatch.setenv("MNG_ROOT_NAME", mng_test_root_name)

    # Unison derives its config directory from $HOME. Since we override HOME
    # above, unison tries to create its config dir inside tmp_path, which
    # fails because the expected parent directories don't exist. The UNISON
    # env var overrides this to a path we control.
    unison_dir = tmp_home_dir / ".unison"
    unison_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("UNISON", str(unison_dir))

    # Safety check: verify Path.home() is in a temp directory.
    # If this fails, tests could accidentally modify the real home directory.
    assert_home_is_temp_directory()

    yield


# =============================================================================
# Modal subprocess test fixtures
# =============================================================================


@pytest.fixture(scope="session")
def modal_test_session_env_name() -> str:
    return generate_test_environment_name()


@pytest.fixture(scope="session")
def modal_test_session_host_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    host_dir = tmp_path_factory.mktemp("modal_session") / "mng"
    host_dir.mkdir(parents=True, exist_ok=True)
    return host_dir


@pytest.fixture(scope="session")
def modal_test_session_user_id() -> UserId:
    return UserId(uuid4().hex)


@pytest.fixture(scope="session")
def modal_test_session_cleanup(
    modal_test_session_env_name: str,
    modal_test_session_user_id: UserId,
) -> Generator[None, None, None]:
    yield
    prefix = f"{modal_test_session_env_name}-"
    environment_name = f"{prefix}{modal_test_session_user_id}"
    if len(environment_name) > 64:
        environment_name = environment_name[:64]
    delete_modal_apps_in_environment(environment_name)
    delete_modal_volumes_in_environment(environment_name)
    delete_modal_environment(environment_name)


@pytest.fixture
def modal_subprocess_env(
    modal_test_session_env_name: str,
    modal_test_session_host_dir: Path,
    modal_test_session_cleanup: None,
    modal_test_session_user_id: UserId,
) -> Generator[ModalSubprocessTestEnv, None, None]:
    prefix = f"{modal_test_session_env_name}-"
    host_dir = modal_test_session_host_dir
    env = get_subprocess_test_env(
        root_name="mng-acceptance-test",
        prefix=prefix,
        host_dir=host_dir,
    )
    env["MNG_USER_ID"] = modal_test_session_user_id
    yield ModalSubprocessTestEnv(env=env, prefix=prefix, host_dir=host_dir)
