"""Shell-completion script generation, installation, and versioning.

This module owns everything about the completion *shell integration* -- as
opposed to ``complete.py``, which is the per-TAB completion engine:

- generating the managed completion function bodies (``generate_zsh_script`` /
  ``generate_bash_script``) and the small rc shim that sources them
  (``generate_*_shim``),
- locating and (re)writing the managed completion files
  (``get_managed_completion_script_path`` / ``write_managed_completion_scripts``),
- the version sentinel + throttled "out of date, refresh me" warning, and
- recognising/removing the old self-contained completion blocks an earlier mngr
  pasted into a user's rc (``strip_legacy_completion_block``).

``mngr extras`` (the install UX) and ``mngr list`` (the background refresh)
orchestrate these; ``complete.py`` calls a couple of them from ``main()``. It is
deliberately stdlib-only (no click/pydantic/plugin imports) so that importing it
on the per-TAB path stays cheap.
"""

import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Final

from imbue.mngr.config.host_dir import read_default_host_dir

# Bump this whenever the generated completion *function* changes in a way that
# warrants users refreshing their installed completion -- in particular when the
# function's contract with the completer changes (e.g. how candidate strings are
# interpreted), since a stale function body paired with the current completer can
# misbehave. The managed completion script sets ``MNGR_COMPLETION_SHIM_VERSION``
# to this value when it invokes the completer; an absent or smaller value means
# the user's installed completion predates the current logic (the old
# self-contained rc function, or a managed file written by an older mngr) and
# should be refreshed via ``mngr extras completion``.
_COMPLETION_SHIM_VERSION: Final[int] = 1
_SHIM_VERSION_ENV_VAR: Final[str] = "MNGR_COMPLETION_SHIM_VERSION"

# How often (seconds) to re-warn about an out-of-date installed completion, so
# the nudge does not appear on every TAB press.
_STALE_WARNING_INTERVAL_SECONDS: Final[float] = 24 * 60 * 60

# Marker comment that identifies the managed completion shim in a user's rc file.
# ``mngr extras`` uses it to tell an up-to-date install (the shim) apart from an
# old self-contained function, and to avoid adding the shim twice.
COMPLETION_SHIM_MARKER: Final[str] = "# mngr shell completion (managed"


def generate_zsh_script() -> str:
    """Generate the zsh completion *function* (the managed file body), python path baked in.

    This is written to the managed completion file (see
    ``write_managed_completion_scripts``); the user's rc only holds the small shim
    from ``generate_zsh_shim`` that sources it.

    Candidates ending in ``.`` or ``=`` are "branch" completions (a dotted config
    key drilled one segment at a time, or a ``KEY=`` whose values come next); they
    are added with an empty suffix (``-S ''``) so no trailing space is inserted and
    the next segment/value can be typed immediately. All other candidates are added
    normally. The completer is invoked with ``MNGR_COMPLETION_SHIM_VERSION`` so it
    can tell an up-to-date install from an out-of-date one.
    """
    python_path = sys.executable
    return f"""_mngr_complete() {{
    local -a completions branches leaves
    (( ! $+commands[mngr] )) && return 1
    completions=(${{(@f)"$(COMP_WORDS="${{words[*]}}" COMP_CWORD=$((CURRENT-1)) {_SHIM_VERSION_ENV_VAR}={_COMPLETION_SHIM_VERSION} {python_path} -m imbue.mngr.cli.complete)"}})
    local c
    for c in $completions; do
        if [[ $c == *. || $c == *= ]]; then branches+=$c; else leaves+=$c; fi
    done
    compadd -U -S '' -V unsorted -a branches
    compadd -U -V unsorted -a leaves
}}
compdef _mngr_complete mngr"""


def generate_bash_script() -> str:
    """Generate the bash completion *function* (the managed file body), python path baked in.

    Written to the managed completion file; the rc only holds the shim from
    ``generate_bash_shim`` that sources it.

    When the sole completion is a "branch" (a dotted config key segment ending in
    ``.``, or a ``KEY=`` whose values come next), suppress the trailing space so the
    next segment/value can be typed immediately. With multiple matches bash already
    inserts only the common prefix (no trailing space), so this is only needed for
    the unique-match case. The completer is invoked with
    ``MNGR_COMPLETION_SHIM_VERSION`` so it can tell an up-to-date install from an
    out-of-date one.
    """
    python_path = sys.executable
    return f"""_mngr_complete() {{
    local IFS=$'\\n'
    COMPREPLY=($(COMP_WORDS="${{COMP_WORDS[*]}}" COMP_CWORD="$COMP_CWORD" {_SHIM_VERSION_ENV_VAR}={_COMPLETION_SHIM_VERSION} {python_path} -m imbue.mngr.cli.complete))
    if [[ ${{#COMPREPLY[@]}} -eq 1 && ( ${{COMPREPLY[0]}} == *. || ${{COMPREPLY[0]}} == *= ) ]]; then
        compopt -o nospace
    fi
}}
complete -o default -F _mngr_complete mngr"""


def _managed_completion_dir() -> Path:
    """Directory holding the managed per-shell completion files."""
    return read_default_host_dir() / "completions"


def get_managed_completion_script_path(shell: str) -> Path:
    """Path of the managed completion file for ``shell`` (e.g. ``~/.mngr/completions/mngr.zsh``)."""
    return _managed_completion_dir() / f"mngr.{shell}"


def generate_zsh_shim() -> str:
    """Generate the small, stable zsh rc snippet that sources the managed completion file.

    This is what goes in the user's ``.zshrc``. It deliberately contains no
    completion logic -- only a pointer to the managed file that mngr regenerates
    -- so completion-logic changes reach the user without editing the rc. The
    path is resolved at shell-startup time the same way mngr resolves the host
    directory (``MNGR_HOST_DIR`` / ``MNGR_ROOT_NAME``), so it keeps working if
    those change.
    """
    return f"""{COMPLETION_SHIM_MARKER}; do not edit -- run `mngr extras completion` to refresh)
typeset _mngr_completion="${{MNGR_HOST_DIR:-$HOME/.${{MNGR_ROOT_NAME:-mngr}}}}/completions/mngr.zsh"
[[ -r "$_mngr_completion" ]] && source "$_mngr_completion"
unset _mngr_completion"""


def generate_bash_shim() -> str:
    """Generate the small, stable bash rc snippet that sources the managed completion file.

    The bash counterpart to ``generate_zsh_shim`` (goes in ``.bashrc``).
    """
    return f"""{COMPLETION_SHIM_MARKER}; do not edit -- run `mngr extras completion` to refresh)
_mngr_completion="${{MNGR_HOST_DIR:-$HOME/.${{MNGR_ROOT_NAME:-mngr}}}}/completions/mngr.bash"
[ -r "$_mngr_completion" ] && . "$_mngr_completion"
unset _mngr_completion"""


def generate_completion_shim(shell: str) -> str:
    """Return the rc shim for ``shell`` (``zsh`` or ``bash``)."""
    if shell == "zsh":
        return generate_zsh_shim()
    return generate_bash_shim()


# The exact self-contained completion functions that mngr generated *before* the
# managed-shim model -- the ``{python_path}`` token marks the only install-specific
# part. ``strip_legacy_completion_block`` removes such a block from a user's rc
# only when it matches one of these byte-for-byte (modulo that path), so migrating
# to the shim never touches a hand-edited completion. (Listed forms: the released
# function, and the segment-drilling form that preceded the shim on this branch.)
#
# DEPRECATED migration shim: mngr stopped generating these self-contained rc
# functions in June 2026 (replaced by the managed shim). This block + the
# templates below + ``strip_legacy_completion_block`` + its call in
# ``extras._install_completion`` exist only to migrate users off the old form.
# Once everyone has had time to upgrade, delete all of it (a leftover old block
# is harmless dead code in a user's rc). Safe to remove entirely after October 2026.
_LEGACY_COMPLETION_FUNCTION_TEMPLATES: Final[tuple[str, ...]] = (
    # zsh, released (pre-segment-drilling)
    """_mngr_complete() {
    local -a completions
    (( ! $+commands[mngr] )) && return 1
    completions=(${(@f)"$(COMP_WORDS="${words[*]}" COMP_CWORD=$((CURRENT-1)) {python_path} -m imbue.mngr.cli.complete)"})
    compadd -U -V unsorted -a completions
}
compdef _mngr_complete mngr""",
    # zsh, segment-drilling (preceded the shim on this branch)
    """_mngr_complete() {
    local -a completions branches leaves
    (( ! $+commands[mngr] )) && return 1
    completions=(${(@f)"$(COMP_WORDS="${words[*]}" COMP_CWORD=$((CURRENT-1)) {python_path} -m imbue.mngr.cli.complete)"})
    local c
    for c in $completions; do
        if [[ $c == *. ]]; then branches+=$c; else leaves+=$c; fi
    done
    compadd -U -S '' -V unsorted -a branches
    compadd -U -V unsorted -a leaves
}
compdef _mngr_complete mngr""",
    # bash, released (pre-segment-drilling)
    """_mngr_complete() {
    local IFS=$'\\n'
    COMPREPLY=($(COMP_WORDS="${COMP_WORDS[*]}" COMP_CWORD="$COMP_CWORD" {python_path} -m imbue.mngr.cli.complete))
}
complete -o default -F _mngr_complete mngr""",
    # bash, segment-drilling (preceded the shim on this branch)
    """_mngr_complete() {
    local IFS=$'\\n'
    COMPREPLY=($(COMP_WORDS="${COMP_WORDS[*]}" COMP_CWORD="$COMP_CWORD" {python_path} -m imbue.mngr.cli.complete))
    if [[ ${#COMPREPLY[@]} -eq 1 && ${COMPREPLY[0]} == *. ]]; then
        compopt -o nospace
    fi
}
complete -o default -F _mngr_complete mngr""",
)


def strip_legacy_completion_block(rc_text: str) -> tuple[str, bool]:
    """Remove an old self-contained mngr completion block from rc text, if it matches a known form.

    Returns ``(new_text, removed)``. A block is removed only when it is
    byte-for-byte one of ``_LEGACY_COMPLETION_FUNCTION_TEMPLATES`` (with any baked
    python path), so a hand-edited or unrecognised completion is left untouched.
    Surrounding blank lines are collapsed so the rc stays tidy.
    """
    for template in _LEGACY_COMPLETION_FUNCTION_TEMPLATES:
        prefix, separator, suffix = template.partition("{python_path}")
        if not separator:
            continue
        # The path is the only variable part of the line; everything else must match.
        pattern = r"\n*" + re.escape(prefix) + r"[^\n]*" + re.escape(suffix) + r"\n*"
        new_text, count = re.subn(pattern, "\n", rc_text, count=1)
        if count:
            return new_text, True
    return rc_text, False


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically write ``content`` to ``path`` (write a temp sibling, then rename).

    A local stdlib-only implementation (rather than ``utils.file_utils.atomic_write``)
    so this module stays free of heavier imports on the tab-completion hot path.
    """
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as tmp_file:
            tmp_file.write(content)
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_managed_completion_scripts() -> None:
    """(Re)write the managed completion files for all supported shells (best-effort).

    These hold the actual completion function; the rc shim just sources them.
    Regenerating here -- e.g. from the background completion refresh -- is what
    lets completion-logic changes reach users without manual rc edits. Only
    rewrites a file whose content changed. Filesystem errors are swallowed so
    this never breaks the caller.
    """
    for shell, content in (("zsh", generate_zsh_script()), ("bash", generate_bash_script())):
        path = get_managed_completion_script_path(shell)
        try:
            if path.is_file() and path.read_text() == content:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(path, content)
        except OSError:
            pass


def _stale_warning_marker_path() -> Path:
    """Timestamp file used to rate-limit the out-of-date-completion warning."""
    return read_default_host_dir() / ".completion-stale-warned"


def maybe_warn_stale_completion() -> None:
    """Warn (throttled, on stderr) if the installed completion predates the current logic.

    The managed completion function sets ``MNGR_COMPLETION_SHIM_VERSION`` when it
    invokes the completer; an absent or smaller value means the user is running
    an out-of-date installed completion (e.g. the old self-contained function
    pasted into their rc) and should refresh it. The warning goes to stderr
    (stdout is reserved for completion candidates) and is rate-limited via a
    marker file (at most once per ``_STALE_WARNING_INTERVAL_SECONDS``) so it does
    not appear on every keypress.
    """
    try:
        installed_version = int(os.environ.get(_SHIM_VERSION_ENV_VAR, "0"))
    except ValueError:
        installed_version = 0
    if installed_version >= _COMPLETION_SHIM_VERSION:
        return

    marker = _stale_warning_marker_path()
    now = time.time()
    try:
        if marker.is_file() and now - marker.stat().st_mtime < _STALE_WARNING_INTERVAL_SECONDS:
            return
    except OSError:
        pass

    sys.stderr.write(
        "\n[mngr] Your shell tab-completion is out of date. Run `mngr extras completion` to refresh it.\n"
    )
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(now))
    except OSError:
        pass
