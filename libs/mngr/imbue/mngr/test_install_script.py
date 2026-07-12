"""Integration tests for scripts/install.sh.

These tests run the install.sh shell script end-to-end against mocked uv and
mngr binaries placed on a synthetic PATH. They verify the script's control
flow -- the single constrained install (with plugin re-listing on upgrade), the
PATH-not-set error, and the continue-on-failure behaviour of steps 3 and 4 --
without requiring a real PyPI package or installed system dependencies.
"""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from imbue.mngr.utils.testing import write_executable_script

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = next(p for p in [_THIS_DIR, *_THIS_DIR.parents] if (p / ".git").exists())
_INSTALL_SH = _REPO_ROOT / "scripts" / "install.sh"
# Resolve bash up-front so the subprocess lookup is independent of the
# minimal PATH we hand the child (which only contains the mock bin dir
# plus a couple of system dirs for grep/printf).
_BASH = shutil.which("bash") or "/bin/bash"


def _uv_mock(log_file: Path, *, mngr_already_installed: bool, installed_plugins: tuple[str, ...]) -> str:
    """Bash mock of `uv` that logs every invocation to `log_file`.

    Supports the subset of commands install.sh actually calls:
    `uv --version`, `uv tool list [--show-with]`, `uv tool install`, `uv tool dir --bin`.
    Anything else returns 0 with no output. When mngr is reported installed,
    `installed_plugins` render as uv's `[with: a, b]` suffix so install.sh can re-list them.
    """
    if mngr_already_installed:
        with_suffix = f" [with: {', '.join(installed_plugins)}]" if installed_plugins else ""
        list_output = f"imbue-mngr v1.0.0{with_suffix}"
    else:
        list_output = ""
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        echo "uv $*" >> "{log_file}"
        case "$1" in
            --version)
                echo "uv 0.5.0"
                ;;
            tool)
                case "$2" in
                    list) echo "{list_output}" ;;
                    dir) echo "/tmp/fake-uv-bin" ;;
                esac
                ;;
        esac
        """
    )


def _mngr_mock(log_file: Path, *, fail_subcommands: tuple[str, ...]) -> str:
    """Bash mock of `mngr` that logs every invocation.

    Every subcommand exits 0 except those in `fail_subcommands`, which are forced
    to exit 1 (e.g. "dependencies"). Pass an empty tuple to make them all succeed.
    """
    fail_cases = "".join(f'    "{cmd}") exit 1 ;;\n' for cmd in fail_subcommands)
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        echo "mngr $*" >> "{log_file}"
        case "$1" in
        {fail_cases}    *) exit 0 ;;
        esac
        """
    )


def _curl_mock(log_file: Path, *, succeeds: bool) -> str:
    """Bash mock of `curl` that logs its invocation and (when succeeding) writes the -o file.

    install.sh fetches the constraints file with `curl -fsSL <url> -o <file>`; this writes a
    placeholder to that path so the subsequent `uv tool install --constraints <file>` has a
    file to read. Pass `succeeds=False` to simulate a failed fetch (non-zero exit, no file).
    """
    body = (
        'out=""\n'
        'while [ "$#" -gt 0 ]; do\n'
        '    case "$1" in\n'
        '        -o) out="$2"; shift 2 ;;\n'
        "        *) shift ;;\n"
        "    esac\n"
        "done\n"
        '[ -n "$out" ] && printf "# test constraints\\n" > "$out"\n'
        "exit 0\n"
        if succeeds
        else "exit 22\n"
    )
    return (
        textwrap.dedent(
            f"""\
        #!/usr/bin/env bash
        echo "curl $*" >> "{log_file}"
        """
        )
        + body
    )


def _make_env(bin_dir: Path, home: Path) -> dict[str, str]:
    # /usr/bin and /bin cover grep / printf on both macOS and Linux. We
    # deliberately do NOT inherit the parent PATH so that the test only
    # exercises the mocks plus standard system utilities.
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
    }


def _run_install_sh(env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_BASH, str(_INSTALL_SH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
    )


def _setup_and_run(
    tmp_path: Path,
    *,
    mngr_already_installed: bool,
    include_mngr_mock: bool,
    fail_subcommands: tuple[str, ...],
    curl_succeeds: bool,
    installed_plugins: tuple[str, ...],
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Write the uv/mngr/curl mocks onto a synthetic PATH, run install.sh, and return the
    completed process together with the text of the call log.

    ``include_mngr_mock=False`` leaves ``mngr`` off PATH, simulating a successful install whose
    bin dir is not on the user's PATH. ``fail_subcommands`` forces those ``mngr`` subcommands to
    exit 1; ``curl_succeeds=False`` simulates a failed constraints fetch; ``installed_plugins``
    are reported by ``uv tool list --show-with`` (only meaningful when already installed).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "calls.log"
    log_file.touch()

    write_executable_script(
        bin_dir / "uv",
        _uv_mock(log_file, mngr_already_installed=mngr_already_installed, installed_plugins=installed_plugins),
    )
    if include_mngr_mock:
        write_executable_script(bin_dir / "mngr", _mngr_mock(log_file, fail_subcommands=fail_subcommands))
    write_executable_script(bin_dir / "curl", _curl_mock(log_file, succeeds=curl_succeeds))

    result = _run_install_sh(env=_make_env(bin_dir, tmp_path), cwd=tmp_path)
    return result, log_file.read_text()


@pytest.mark.timeout(30)
def test_install_sh_installs_at_latest_without_unconstrained_upgrade(tmp_path: Path) -> None:
    """Already-installed mngr is upgraded and re-pinned by a single constrained
    `uv tool install imbue-mngr@latest` -- never a separate unconstrained `uv tool upgrade`,
    which would briefly resolve dependencies unpinned (bypassing the tested-versions pin)."""
    result, calls = _setup_and_run(
        tmp_path,
        mngr_already_installed=True,
        include_mngr_mock=True,
        fail_subcommands=(),
        curl_succeeds=True,
        installed_plugins=(),
    )

    assert result.returncode == 0, f"install.sh failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "curl" in calls and "constraints.txt" in calls
    assert "uv tool install imbue-mngr@latest --constraints" in calls
    # A separate `uv tool upgrade` bypasses the constraints, so it must not be used.
    assert "uv tool upgrade" not in calls
    assert "mngr dependencies --install interactive --scope core" in calls
    assert "mngr extras -i" in calls
    assert "mngr config wizard" in calls


@pytest.mark.timeout(30)
def test_install_sh_installs_when_mngr_not_present(tmp_path: Path) -> None:
    """When uv reports no imbue-mngr, run `uv tool install` (not upgrade)."""
    result, calls = _setup_and_run(
        tmp_path,
        mngr_already_installed=False,
        include_mngr_mock=True,
        fail_subcommands=(),
        curl_succeeds=True,
        installed_plugins=(),
    )

    assert result.returncode == 0, f"install.sh failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    # The constraints file is fetched, then a single constrained @latest install runs.
    assert "curl" in calls and "constraints.txt" in calls
    assert "uv tool install imbue-mngr@latest --constraints" in calls
    assert "uv tool upgrade" not in calls
    assert "mngr dependencies --install interactive --scope core" in calls
    assert "mngr extras -i" in calls
    assert "mngr config wizard" in calls


@pytest.mark.timeout(30)
def test_install_sh_errors_when_mngr_not_on_path_after_install(tmp_path: Path) -> None:
    """If `command -v mngr` fails after `uv tool install`, exit with a PATH error.

    Simulates the post-install state where mngr is not resolvable on $PATH
    by leaving the mock `mngr` binary off the synthetic PATH entirely.
    install.sh cannot distinguish "binary lives in a directory not on PATH"
    from "binary was never written" -- both reach the same `command -v mngr`
    check -- so this test exercises the PATH-error branch of install.sh
    without modelling where uv would have written the binary.
    """
    # uv mock present, but no mngr binary on PATH -- simulates a successful
    # install whose bin dir is not on the user's PATH.
    result, calls = _setup_and_run(
        tmp_path,
        mngr_already_installed=False,
        include_mngr_mock=False,
        fail_subcommands=(),
        curl_succeeds=True,
        installed_plugins=(),
    )

    assert result.returncode != 0
    assert "uv tool install imbue-mngr" in calls
    assert "uv tool dir --bin" in calls
    # Pin to install.sh's error wording so an unrelated non-zero exit
    # whose stderr happens to mention PATH does not satisfy this test.
    assert "is not on your PATH" in result.stderr


@pytest.mark.timeout(30)
def test_install_sh_continues_when_dependencies_fail(tmp_path: Path) -> None:
    """A failure in `mngr dependencies --install interactive` must not abort step 4 (`mngr extras -i`).

    The `|| warn` pattern in install.sh exists so a single broken system
    dependency does not stop the rest of the installer.
    """
    result, calls = _setup_and_run(
        tmp_path,
        mngr_already_installed=True,
        include_mngr_mock=True,
        fail_subcommands=("dependencies",),
        curl_succeeds=True,
        installed_plugins=(),
    )

    assert result.returncode == 0, f"install.sh failed unexpectedly\nstderr:\n{result.stderr}"
    assert "mngr dependencies --install interactive --scope core" in calls
    assert "mngr extras -i" in calls
    # Pin the assertion to the step-3 warning text from install.sh so a
    # regression that fires the wrong || warn (or none at all) is caught.
    assert "Some dependencies could not be installed" in result.stderr


@pytest.mark.timeout(30)
def test_install_sh_continues_when_extras_fail(tmp_path: Path) -> None:
    """A failure in `mngr extras -i` must not abort the script."""
    result, calls = _setup_and_run(
        tmp_path,
        mngr_already_installed=True,
        include_mngr_mock=True,
        fail_subcommands=("extras",),
        curl_succeeds=True,
        installed_plugins=(),
    )

    assert result.returncode == 0, f"install.sh failed unexpectedly\nstderr:\n{result.stderr}"
    assert "mngr extras -i" in calls
    # Pin the assertion to the step-4 warning text from install.sh so a
    # regression that fires the wrong || warn (or none at all) is caught.
    assert "Some extras could not be installed" in result.stderr
    # The wizard step runs after extras even when extras failed.
    assert "mngr config wizard" in calls
    # Pin to install.sh's exact final-line text so a refactor that silently
    # drops the line in favour of something else is caught here.
    assert "Get started with: mngr --help" in result.stdout


@pytest.mark.timeout(30)
def test_install_sh_continues_when_config_wizard_fails(tmp_path: Path) -> None:
    """A failure in `mngr config wizard` (step 5) must not abort the script."""
    result, calls = _setup_and_run(
        tmp_path,
        mngr_already_installed=True,
        include_mngr_mock=True,
        fail_subcommands=("config",),
        curl_succeeds=True,
        installed_plugins=(),
    )

    assert result.returncode == 0, f"install.sh failed unexpectedly\nstderr:\n{result.stderr}"
    assert "mngr config wizard" in calls
    # Pin the assertion to the step-5 warning text from install.sh.
    assert "Configuration wizard did not complete" in result.stderr
    assert "Get started with: mngr --help" in result.stdout


@pytest.mark.timeout(30)
def test_install_sh_aborts_when_constraints_fetch_fails(tmp_path: Path) -> None:
    """A failed constraints fetch aborts the installer before mngr is installed.

    Pinning to the tested versions is required, not best-effort: if the constraints file
    cannot be fetched, `set -euo pipefail` + `curl -f` abort the script rather than silently
    installing an unpinned mngr.
    """
    result, calls = _setup_and_run(
        tmp_path,
        mngr_already_installed=False,
        include_mngr_mock=True,
        fail_subcommands=(),
        curl_succeeds=False,
        installed_plugins=(),
    )

    assert result.returncode != 0, f"expected abort on failed fetch\nstdout:\n{result.stdout}"
    assert "curl" in calls
    # mngr must NOT have been installed when the constraints could not be fetched.
    assert "uv tool install imbue-mngr" not in calls


@pytest.mark.timeout(30)
def test_install_sh_relists_plugins_when_already_installed(tmp_path: Path) -> None:
    """An upgrade re-lists existing plugins as ``--with`` in the single constrained install so they
    survive -- a bare ``uv tool install imbue-mngr`` would reset the tool to just its base package."""
    result, calls = _setup_and_run(
        tmp_path,
        mngr_already_installed=True,
        include_mngr_mock=True,
        fail_subcommands=(),
        curl_succeeds=True,
        installed_plugins=("imbue-mngr-modal", "imbue-mngr-claude"),
    )

    assert result.returncode == 0, f"install.sh failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "uv tool upgrade" not in calls
    # The constrained install must carry the existing plugins, or the upgrade would drop them.
    assert "uv tool install imbue-mngr@latest --with imbue-mngr-modal --with imbue-mngr-claude --constraints" in calls
