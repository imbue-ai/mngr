import hashlib
import sys
from pathlib import Path

import pytest

from imbue.mngr.cli.completion_install import COMPLETION_SHIM_MARKER
from imbue.mngr.cli.completion_install import _COMPLETION_SHIM_VERSION
from imbue.mngr.cli.completion_install import _SHIM_VERSION_ENV_VAR
from imbue.mngr.cli.completion_install import generate_bash_script
from imbue.mngr.cli.completion_install import generate_bash_shim
from imbue.mngr.cli.completion_install import generate_zsh_script
from imbue.mngr.cli.completion_install import generate_zsh_shim
from imbue.mngr.cli.completion_install import get_managed_completion_script_path
from imbue.mngr.cli.completion_install import maybe_warn_stale_completion
from imbue.mngr.cli.completion_install import strip_legacy_completion_block
from imbue.mngr.cli.completion_install import write_managed_completion_scripts

# =============================================================================
# Managed completion files, rc shim, and stale-completion warning
# =============================================================================


def test_completion_shim_sources_managed_file_zsh() -> None:
    """The zsh rc shim is a thin pointer (marker + source of the managed file), not the function."""
    shim = generate_zsh_shim()
    assert COMPLETION_SHIM_MARKER in shim
    assert "completions/mngr.zsh" in shim
    # No completion logic inlined -- that lives in the managed file.
    assert "compadd" not in shim


def test_completion_shim_sources_managed_file_bash() -> None:
    """The bash rc shim is a thin pointer to the managed file, not the function."""
    shim = generate_bash_shim()
    assert COMPLETION_SHIM_MARKER in shim
    assert "completions/mngr.bash" in shim
    assert "complete -o default" not in shim


def test_managed_script_carries_version_sentinel() -> None:
    """The managed completion function invokes the completer with the shim-version env var.

    This is what lets the completer recognise an up-to-date install vs an old one.
    """
    script = generate_zsh_script()
    assert f"{_SHIM_VERSION_ENV_VAR}={_COMPLETION_SHIM_VERSION}" in script


# Fingerprint of the generated zsh+bash completion function bodies, with the baked
# python path normalised out (it varies by environment). Pinned so a change to the
# generated function can't land silently: such a change alters the contract with the
# completer (how candidate strings are interpreted), so it must be paired with a bump
# of ``_COMPLETION_SHIM_VERSION`` -- which keeps out-of-date installs getting the
# refresh nudge. See ``test_completion_function_change_requires_version_bump``.
_EXPECTED_COMPLETION_FUNCTION_FINGERPRINT = "395ccc884fbb8fbe0e30d50022128f2bdfdb14785521456e368d29cf524f44aa"


def _completion_function_fingerprint() -> str:
    """sha256 of the generated zsh+bash function bodies, with the baked python path removed."""
    bodies = (generate_zsh_script() + "\n" + generate_bash_script()).replace(sys.executable, "PYTHON")
    return hashlib.sha256(bodies.encode("utf-8")).hexdigest()


def test_completion_function_change_requires_version_bump() -> None:
    """Changing the generated completion function must be accompanied by a shim-version bump.

    The generated zsh/bash function bodies define a contract with the completer
    (how trailing ``.``/``=`` candidates are treated, the candidate shape, etc.).
    If a body changes but ``_COMPLETION_SHIM_VERSION`` does not, an already-installed
    function can silently misbehave against the current completer instead of
    prompting the user to refresh. This pins a fingerprint of the bodies; if it
    fails because you changed the generated function:

    1. Bump ``_COMPLETION_SHIM_VERSION`` in ``completion_install.py`` (so stale
       installs get the refresh nudge).
    2. Update ``_EXPECTED_COMPLETION_FUNCTION_FINGERPRINT`` to the new value printed
       in the assertion message below.
    """
    current = _completion_function_fingerprint()
    assert current == _EXPECTED_COMPLETION_FUNCTION_FINGERPRINT, (
        "The generated completion function changed. Bump _COMPLETION_SHIM_VERSION in "
        "completion_install.py (so out-of-date installs get the refresh nudge), then set "
        f"_EXPECTED_COMPLETION_FUNCTION_FINGERPRINT = {current!r}"
    )


def test_write_managed_completion_scripts_writes_both_shells(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """write_managed_completion_scripts writes the function file for each shell under the host dir."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    write_managed_completion_scripts()

    zsh_path = get_managed_completion_script_path("zsh")
    bash_path = get_managed_completion_script_path("bash")
    assert zsh_path.is_file()
    assert bash_path.is_file()
    assert "_mngr_complete" in zsh_path.read_text()
    assert "_mngr_complete" in bash_path.read_text()

    # Idempotent: a second call leaves identical content (write-if-changed).
    before = zsh_path.read_text()
    write_managed_completion_scripts()
    assert zsh_path.read_text() == before


def test_maybe_warn_stale_completion_warns_for_old_shim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no shim-version env (an old install), a throttled warning is written to stderr."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.delenv(_SHIM_VERSION_ENV_VAR, raising=False)

    maybe_warn_stale_completion()
    captured = capsys.readouterr()
    assert "out of date" in captured.err
    assert captured.out == ""

    # Throttled: an immediate second call stays silent.
    maybe_warn_stale_completion()
    assert "out of date" not in capsys.readouterr().err


def test_maybe_warn_stale_completion_silent_for_current_shim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An up-to-date shim (current version env) produces no warning."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv(_SHIM_VERSION_ENV_VAR, str(_COMPLETION_SHIM_VERSION))

    maybe_warn_stale_completion()
    assert capsys.readouterr().err == ""


# =============================================================================
# Legacy completion block removal (migration to the managed shim)
# =============================================================================


_LEGACY_ZSH_BLOCK = """_mngr_complete() {
    local -a completions
    (( ! $+commands[mngr] )) && return 1
    completions=(${(@f)"$(COMP_WORDS="${words[*]}" COMP_CWORD=$((CURRENT-1)) /home/u/.venv/bin/python3 -m imbue.mngr.cli.complete)"})
    compadd -U -V unsorted -a completions
}
compdef _mngr_complete mngr"""


def test_strip_legacy_completion_block_removes_known_block() -> None:
    """A byte-identical old completion block (any python path) is removed, surrounding text kept."""
    rc = f"# my config\nexport FOO=1\n\n{_LEGACY_ZSH_BLOCK}\n\nalias x=y\n"

    new_text, removed = strip_legacy_completion_block(rc)

    assert removed is True
    assert "_mngr_complete" not in new_text
    assert "export FOO=1" in new_text
    assert "alias x=y" in new_text


def test_strip_legacy_completion_block_keeps_hand_edited_block() -> None:
    """A block that is not byte-identical to a known form (hand-edited) is left untouched."""
    # Any byte difference from the known template (here, dropping ``-V unsorted``)
    # means it is not auto-removed.
    edited = _LEGACY_ZSH_BLOCK.replace("compadd -U -V unsorted -a completions", "compadd -U -a completions")
    rc = f"# config\n{edited}\n"

    new_text, removed = strip_legacy_completion_block(rc)

    assert removed is False
    assert new_text == rc


def test_strip_legacy_completion_block_no_block() -> None:
    """rc with no mngr completion is returned unchanged."""
    rc = "# just some config\nexport FOO=1\n"

    new_text, removed = strip_legacy_completion_block(rc)

    assert removed is False
    assert new_text == rc


def test_strip_legacy_completion_block_does_not_touch_the_shim() -> None:
    """The managed shim itself is not a legacy block, so it is preserved."""
    shim = generate_zsh_shim()
    rc = f"# config\n{shim}\n"

    new_text, removed = strip_legacy_completion_block(rc)

    assert removed is False
    assert COMPLETION_SHIM_MARKER in new_text
