"""The Linux installer's WSL step that sends links to the Windows default browser, run on stock Ubuntu 24.04.

WSL's Ubuntu image ships without xdg-utils or any browser, which is why the app's sign-ins opened
nothing there. The container stands in for the distro, with a fake ``rundll32.exe`` on PATH that
records the arguments Windows would have received.
"""

import re
import subprocess
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import pytest

_INSTALL_LINUX_PATH: Final[Path] = Path(__file__).parent / "install-linux.sh"
_HANDLER_FUNCTION_NAME: Final[str] = "register_windows_browser_handler"
# What `wsl --install` installs by default.
_DISTRO_IMAGE: Final[str] = "ubuntu:24.04"
_FAKE_WINDOWS_DIR: Final[str] = "/fakewin"
_RUNDLL32_ARGS_FILE: Final[str] = "/tmp/rundll32-args"
_PATH: Final[str] = f"{_FAKE_WINDOWS_DIR}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# The shape of a real sign-in URL: an encoded loopback callback, and & and = that a shell would split on.
_SIGN_IN_URL: Final[str] = (
    "https://accounts.example.com/login?redirect_uri=http%3A%2F%2F127.0.0.1%3A5123%2Fcallback&state=a=b&x=1"
)
_ENTRY_PATH: Final[str] = ".local/share/applications/imbue-studio-windows-browser.desktop"


def _handler_function_source() -> str:
    match = re.search(
        rf"^{_HANDLER_FUNCTION_NAME}\(\) \{{\n.*?^\}}\n", _INSTALL_LINUX_PATH.read_text(), re.MULTILINE | re.DOTALL
    )
    assert match is not None, f"install-linux.sh no longer defines {_HANDLER_FUNCTION_NAME} at top level"
    return match.group(0)


@contextmanager
def _distro_container(work_dir: Path) -> Generator[str, None, None]:
    name = f"wsl-browser-test-{uuid.uuid4().hex}"
    started = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-v", f"{work_dir}:/work:ro", _DISTRO_IMAGE, "sleep", "600"],
        capture_output=True,
        text=True,
    )
    assert started.returncode == 0, started.stderr
    try:
        yield name
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _exec(container: str, command: str, user: str = "root") -> subprocess.CompletedProcess[str]:
    home = "/root" if user == "root" else f"/home/{user}"
    return subprocess.run(
        ["docker", "exec", "-u", user, "-e", f"HOME={home}", "-e", f"PATH={_PATH}", "-e", "DISPLAY=:0"]
        + [container, "bash", "-c", command],
        capture_output=True,
        text=True,
    )


def _run_handler(container: str, user: str) -> str:
    result = _exec(
        container,
        'set -euo pipefail; step() { echo "==> $*"; }; skip() { echo "(already done: $*)"; }; '
        f". /work/handler.sh; {_HANDLER_FUNCTION_NAME}",
        user=user,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _rundll32_args_after(container: str, user: str, open_command: str) -> list[str]:
    """Run ``open_command`` and return what rundll32 received (empty when it was never called)."""
    _exec(container, f"rm -f {_RUNDLL32_ARGS_FILE}")
    _exec(container, open_command, user=user)
    # webbrowser starts xdg-open in the background, so wait for it to finish handing the URL over.
    _exec(container, f"for _ in $(seq 50); do [ -s {_RUNDLL32_ARGS_FILE} ] && break; sleep 0.1; done")
    recorded = _exec(container, f"cat {_RUNDLL32_ARGS_FILE} 2>/dev/null || true").stdout
    return recorded.splitlines()


@pytest.mark.docker
@pytest.mark.acceptance
# MIND-790: the setup's apt download into a fresh container has outlasted the 300 s budget.
@pytest.mark.flaky
@pytest.mark.timeout(300)
def test_wsl_installer_sends_links_to_the_windows_browser_unless_the_distro_has_one(tmp_path: Path) -> None:
    (tmp_path / "handler.sh").write_text(_handler_function_source())
    (tmp_path / "rundll32.exe").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {_RUNDLL32_ARGS_FILE}\n')
    tmp_path.chmod(0o755)
    with _distro_container(tmp_path) as container:
        setup = _exec(
            container,
            "set -e; apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xdg-utils python3;"
            f" mkdir {_FAKE_WINDOWS_DIR}; install -m 755 /work/rundll32.exe {_FAKE_WINDOWS_DIR}/;"
            " useradd -m fresh; useradd -m has-browser",
        )
        assert setup.returncode == 0, setup.stderr

        xdg_open = f"xdg-open '{_SIGN_IN_URL}'"
        python_webbrowser = f"python3 -c 'import sys, webbrowser; webbrowser.open(sys.argv[1])' '{_SIGN_IN_URL}'"
        assert _rundll32_args_after(container, "fresh", xdg_open) == []

        assert "links now open in the Windows default browser" in _run_handler(container, "fresh")
        expected_args = ["url.dll,FileProtocolHandler", _SIGN_IN_URL]
        assert _rundll32_args_after(container, "fresh", xdg_open) == expected_args
        assert _rundll32_args_after(container, "fresh", python_webbrowser) == expected_args
        assert "(already done: links open in the Windows default browser)" in _run_handler(container, "fresh")

        distro_browser = _exec(
            container,
            "mkdir -p ~/.local/share/applications"
            " && printf '[Desktop Entry]\\nType=Application\\nName=Firefox\\nExec=/bin/true %%u\\n'"
            " > ~/.local/share/applications/firefox.desktop"
            " && xdg-mime default firefox.desktop x-scheme-handler/https",
            user="has-browser",
        )
        assert distro_browser.returncode == 0, distro_browser.stderr
        assert "keeping firefox.desktop" in _run_handler(container, "has-browser")
        assert _exec(container, f"test -e ~/{_ENTRY_PATH}", user="has-browser").returncode == 1
        assert _rundll32_args_after(container, "has-browser", xdg_open) == []
