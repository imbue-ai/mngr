import subprocess
import sys
from typing import Final

# Each script runs in its own interpreter, because it hides pyinfra's installed metadata by
# taking site-packages off sys.path -- what a venv rewritten under a running process looks like
# to a fresh lookup -- and that must never reach the test process itself.
_HIDE_SITE_PACKAGES: Final[str] = """
import site
import sys

_FULL_SYS_PATH = list(sys.path)
_SITE_PACKAGES = set(site.getsitepackages())


def hide_site_packages():
    sys.path[:] = [entry for entry in _FULL_SYS_PATH if entry not in _SITE_PACKAGES]


def restore_site_packages():
    sys.path[:] = _FULL_SYS_PATH
"""

_RETRY_AFTER_AN_EMPTY_LOOKUP_SCRIPT: Final[str] = (
    _HIDE_SITE_PACKAGES
    + """
from imbue.mngr.hosts.pyinfra_host_builder import build_pyinfra_host

hide_site_packages()
try:
    build_pyinfra_host("@local")
except KeyError:
    is_built_while_hidden = False
else:
    is_built_while_hidden = True
assert not is_built_while_hidden, "built a host while pyinfra's metadata was hidden"
restore_site_packages()
build_pyinfra_host("@local")
"""
)


_BUILD_AFTER_THE_METADATA_VANISHES_SCRIPT: Final[str] = (
    _HIDE_SITE_PACKAGES
    + """
import importlib.metadata

from imbue.mngr.hosts.pyinfra_host_builder import build_pyinfra_host

build_pyinfra_host("@local")
hide_site_packages()
assert not importlib.metadata.entry_points(group="pyinfra.connectors"), "pyinfra's metadata is still reachable"
build_pyinfra_host("@local")
build_pyinfra_host("host.example.com", {"ssh_user": "someone"})
"""
)


# Exit code the script uses to report the expected error, so no other outcome can pass for it.
_RAISED_AS_A_MNGR_ERROR_EXIT_CODE: Final[int] = 17

_BUILD_WITHOUT_A_REACHABLE_CONNECTOR_SCRIPT: Final[str] = (
    _HIDE_SITE_PACKAGES
    + f"""
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import PyinfraConnectorUnavailableError
from imbue.mngr.hosts.pyinfra_host_builder import build_pyinfra_host

hide_site_packages()
try:
    build_pyinfra_host("@local")
except PyinfraConnectorUnavailableError as e:
    if isinstance(e, MngrError) and isinstance(e.__cause__, KeyError):
        sys.exit({_RAISED_AS_A_MNGR_ERROR_EXIT_CODE})
    raise
"""
)


def _run_script(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)


def test_hosts_keep_building_after_pyinfra_metadata_vanishes_from_disk() -> None:
    result = _run_script(_BUILD_AFTER_THE_METADATA_VANISHES_SCRIPT)

    assert result.returncode == 0, result.stderr


def test_a_host_with_no_reachable_connector_raises_a_mngr_error_naming_it() -> None:
    result = _run_script(_BUILD_WITHOUT_A_REACHABLE_CONNECTOR_SCRIPT)

    assert result.returncode == _RAISED_AS_A_MNGR_ERROR_EXIT_CODE, result.stderr
    assert "pyinfra has no 'ssh' connector" in result.stderr
    assert "connector_names=()" in result.stderr


def test_a_lookup_that_found_no_connectors_is_retried_once_they_are_back() -> None:
    result = _run_script(_RETRY_AFTER_AN_EMPTY_LOOKUP_SCRIPT)

    assert result.returncode == 0, result.stderr
