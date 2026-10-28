import os
import subprocess
import sys
from pathlib import Path
from typing import Final

# Exit code the script uses to report the expected error, so no other outcome can pass for it.
_RAISED_FROM_KEY_ERROR_EXIT_CODE: Final[int] = 17

_CREATE_HOST_SCRIPT = f"""
import sys
from pathlib import Path

from imbue.mngr.errors import PyinfraConnectorUnavailableError
from imbue.mngr.providers.ssh_utils import create_pyinfra_host

try:
    create_pyinfra_host(
        hostname="host.example.com",
        port=22,
        private_key_path=Path("unused_key"),
        known_hosts_path=Path("unused_known_hosts"),
    )
except PyinfraConnectorUnavailableError as e:
    if isinstance(e, KeyError) and isinstance(e.__cause__, KeyError):
        sys.exit({_RAISED_FROM_KEY_ERROR_EXIT_CODE})
    raise
"""


def test_create_pyinfra_host_reports_a_shadowed_pyinfra_install_as_a_mngr_error(tmp_path: Path) -> None:
    """Metadata for pyinfra with no entry points, ahead of the real one on sys.path, hides every connector.

    Runs in a subprocess so the shadowing entry on sys.path never reaches this test process.
    """
    stub_dir = tmp_path / "stub"
    dist_info_dir = stub_dir / "pyinfra-99.0.dist-info"
    dist_info_dir.mkdir(parents=True)
    (dist_info_dir / "METADATA").write_text("Metadata-Version: 2.1\nName: pyinfra\nVersion: 99.0\n")

    result = subprocess.run(
        [sys.executable, "-c", _CREATE_HOST_SCRIPT],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(stub_dir), os.environ.get("PYTHONPATH")]))},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == _RAISED_FROM_KEY_ERROR_EXIT_CODE, result.stderr
    assert "pyinfra has no 'ssh' connector" in result.stderr
    assert f"distribution_location=PosixPath('{stub_dir}')" in result.stderr
    assert "is_distribution_beside_module=False" in result.stderr
    assert "connector_names=()" in result.stderr
