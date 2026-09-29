"""Translate MINDS_ROOT_NAME into MNGR_HOST_DIR and MNGR_PREFIX.

This must run before any ``imbue.mngr.*`` module is imported, because mngr reads ``MNGR_HOST_DIR`` and ``MNGR_PREFIX`` during its own module-level initialization (plugin manager construction, config discovery, etc.).

Kept intentionally minimal -- stdlib plus ``imbue.imbue_common.enums`` -- so it stays cheap to import and cannot accidentally pull in mngr before translation happens.
The repo-root ``pyproject.toml``'s import-linter contract "minds bootstrap layer stays mngr-free and import-cheap" declares this, and forbids ``pydantic`` here as well as ``imbue.mngr`` -- which is why :class:`MindsRoot` is a plain class and why path resolution is a function rather than a value object.
The settings-file machinery that shares this constraint lives in :mod:`imbue.minds.mngr_settings`.
"""

import os
import re
import sys
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never

from imbue.imbue_common.enums import UpperCaseStrEnum

MINDS_ROOT_NAME_ENV_VAR: Final[str] = "MINDS_ROOT_NAME"
DEFAULT_MINDS_ROOT_NAME: Final[str] = "minds"
_MINDS_PREFIX: Final[str] = "minds"
# Legal env-name suffixes after ``minds-``.
# Mirrors the rules in :mod:`imbue.minds.envs.primitives` and the reserved tier names the operator env CLI accepts:
#
#   * ``staging`` -- the reserved staging tier name.
#   * ``dev-<rest>`` / ``ci-<rest>`` -- any dynamic env (developer dev env or CI ephemeral env, respectively).
#     :data:`DYNAMIC_ENV_NAME_PATTERN` is the single source of this shape; ``imbue.minds.envs.primitives`` imports it as ``DEV_ENV_NAME_PATTERN``.
#
# Production has no suffix (``minds`` alone).
# Anything set but not matching this pattern makes ``resolve_minds_root_name`` raise.
_STAGING_SUFFIX_PATTERN: Final[str] = r"staging"
# The user portion's max length (34 between the two anchor characters) keeps the total ``<tier>-<user>`` name within ``MAX_DEV_ENV_NAME_LENGTH`` (40).
DYNAMIC_ENV_NAME_PATTERN: Final[str] = r"(?:dev|ci)-[a-z0-9][a-z0-9_-]{0,34}[a-z0-9]"
_ENV_NAME_PATTERN: Final[str] = rf"(?:{_STAGING_SUFFIX_PATTERN}|{DYNAMIC_ENV_NAME_PATTERN})"
# The full set of legal MINDS_ROOT_NAME values is ``minds`` (production), ``minds-staging``, ``minds-dev-<rest>``, or ``minds-ci-<rest>``.
MINDS_ROOT_NAME_PATTERN: Final[str] = rf"{_MINDS_PREFIX}(-{_ENV_NAME_PATTERN})?"

# The application name the platform-canonical roots are keyed on, e.g. ``~/Library/Application Support/Imbue Studio``.
# Matches ``productName`` in ``apps/minds/package.json``, which is what Electron derives its own default ``userData`` path from.
MINDS_APP_NAME: Final[str] = "Imbue Studio"

# Points all three roots at one throwaway directory, for tests and the CI runner.
MINDS_DATA_HOME_ENV_VAR: Final[str] = "MINDS_DATA_HOME"

# The per-role roots a parent process can hand this one, which win over anything resolved here.
# The Electron shell is the intended setter: it creates the virtualenv before any Python exists to ask, so it owns resolution (see specs/minds-platform-canonical-dirs/spec.md, F7).
MINDS_STATE_DIR_ENV_VAR: Final[str] = "MINDS_STATE_DIR"
MINDS_CACHE_DIR_ENV_VAR: Final[str] = "MINDS_CACHE_DIR"
MINDS_LOG_DIR_ENV_VAR: Final[str] = "MINDS_LOG_DIR"
_INJECTED_ROOT_ENV_VARS: Final[tuple[str, ...]] = (
    MINDS_STATE_DIR_ENV_VAR,
    MINDS_CACHE_DIR_ENV_VAR,
    MINDS_LOG_DIR_ENV_VAR,
)

# The one platform with its own canonical roots; everywhere else keeps the single dotfolder.
_APPLE_PLATFORM: Final[str] = "darwin"

# The logs subdirectory of a dotfolder root. Matches LOGS_ENTRY_NAME in electron/platform-roots.js.
_DOTFOLDER_LOGS_SUBDIR: Final[str] = "logs"


class BootstrapError(ValueError):
    """Raised when the Imbue Studio bootstrap layer can't compute a derived value.

    Defined here instead of in ``minds.errors`` because this module has to stay free of any ``imbue.mngr.*`` / ``click`` imports (see the module docstring).
    """


def resolve_minds_root_name() -> str:
    """Read MINDS_ROOT_NAME from the environment or return the default.

    When the env var is unset, returns :data:`DEFAULT_MINDS_ROOT_NAME` (production).
    When the env var is set to a value that does not match :data:`MINDS_ROOT_NAME_PATTERN` (e.g. a stale ``devminds`` left in a parent shell from before the per-env-root refactor), raises ``BootstrapError`` -- silently coercing to production would point tooling at production data the user did not ask for.

    Validation is duplicated here (instead of going through a pydantic primitive) so this module never has to import pydantic/mngr.
    """
    value = os.environ.get(MINDS_ROOT_NAME_ENV_VAR)
    if value is None:
        return DEFAULT_MINDS_ROOT_NAME
    if not re.fullmatch(MINDS_ROOT_NAME_PATTERN, value):
        raise BootstrapError(
            f"{MINDS_ROOT_NAME_ENV_VAR}={value!r} does not match {MINDS_ROOT_NAME_PATTERN!r}. "
            f"Run `unset {MINDS_ROOT_NAME_ENV_VAR}` to use production, or set it to a valid env name."
        )
    return value


def default_root_name_to_production() -> None:
    """Seed ``MINDS_ROOT_NAME`` with :data:`DEFAULT_MINDS_ROOT_NAME` when it is unset.

    Run before :func:`apply_bootstrap` so a bare source run owns the whole production root
    (data dir, mngr host dir and prefix, Sentry target), not just the production client config
    that :func:`imbue.minds.config.loader.resolve_client_config_path` defaults to. A set value,
    valid or not, is left alone.
    """
    os.environ.setdefault(MINDS_ROOT_NAME_ENV_VAR, DEFAULT_MINDS_ROOT_NAME)


def is_env_activated() -> bool:
    """Return whether ``MINDS_ROOT_NAME`` is set in the environment.

    An unset value means the shell has not explicitly activated any env, which is what callers that must not silently fall back to production gate on; ``MINDS_ROOT_NAME=minds`` counts as an explicit activation of production.
    Raises ``BootstrapError`` (via :func:`resolve_minds_root_name`) when the value is set but invalid.
    """
    if os.environ.get(MINDS_ROOT_NAME_ENV_VAR) is None:
        return False
    resolve_minds_root_name()
    return True


def env_name_from_root_name(root_name: str) -> str:
    """Return the env name for a given ``MINDS_ROOT_NAME``.

    ``minds`` -> ``production``; ``minds-<name>`` -> ``<name>``.
    Raises ``BootstrapError`` for any other value -- callers should validate via :func:`resolve_minds_root_name` first.
    """
    if root_name == DEFAULT_MINDS_ROOT_NAME:
        return "production"
    if not root_name.startswith(f"{_MINDS_PREFIX}-"):
        raise BootstrapError(
            f"Cannot extract env name from {MINDS_ROOT_NAME_ENV_VAR}={root_name!r}: "
            f"expected {DEFAULT_MINDS_ROOT_NAME!r} or {_MINDS_PREFIX}-<env-name>."
        )
    return root_name[len(_MINDS_PREFIX) + 1 :]


def root_name_for_env_name(env_name: str) -> str:
    """Return the ``MINDS_ROOT_NAME`` value for a given env name.

    ``production`` -> ``minds``; anything else -> ``minds-<name>``.
    The env name is not re-validated here; callers should validate via :class:`imbue.minds.envs.primitives.DevEnvName` first.
    """
    if env_name == "production":
        return DEFAULT_MINDS_ROOT_NAME
    return f"{_MINDS_PREFIX}-{env_name}"


def minds_data_dir_for(root_name: str) -> Path:
    """Return the ``~/.<root_name>`` dotfolder root (e.g. ~/.minds).

    On macOS this is the pre-migration root, which only the migration still reads; everywhere else it is the live root.
    """
    return Path.home() / ".{}".format(root_name)


def mngr_host_dir_for(root_name: str) -> Path:
    """Return the mngr host directory for a given root name, under the tier's state root."""
    return minds_dir_for_role(MindsPathRole.STATE, root_name) / "mngr"


def mngr_prefix_for(root_name: str) -> str:
    """Return the mngr prefix for a given root name (e.g. minds-)."""
    return "{}-".format(root_name)


class MindsPathRole(UpperCaseStrEnum):
    """One of the platform-canonical roots a minds tier keeps its files under.

    Three roles, not four: macOS files data, config, and state under one ``Application Support`` directory, so a config/state split is unobservable on the only platform minds ships.
    """

    STATE = auto()
    CACHE = auto()
    LOGS = auto()


# The variable the Electron shell hands each already-resolved, already-tier-qualified root down through.
_INJECTED_ENV_VAR_BY_ROLE: Final[dict[MindsPathRole, str]] = {
    MindsPathRole.STATE: MINDS_STATE_DIR_ENV_VAR,
    MindsPathRole.CACHE: MINDS_CACHE_DIR_ENV_VAR,
    MindsPathRole.LOGS: MINDS_LOG_DIR_ENV_VAR,
}

# Each role's directory name under a ``MINDS_DATA_HOME`` tier root.
_DATA_HOME_SUBDIR_BY_ROLE: Final[dict[MindsPathRole, str]] = {
    MindsPathRole.STATE: "state",
    MindsPathRole.CACHE: "cache",
    MindsPathRole.LOGS: "logs",
}


def _check_injected_roots_are_complete() -> None:
    """Reject a partial set of injected roots; all three or none.

    A partial set means the shell and the backend disagree about the layout, which would scatter one tier's files across two of them. That is a bug in the launcher rather than something to paper over by filling the gaps in from the platform default.
    """
    present = tuple(name for name in _INJECTED_ROOT_ENV_VARS if os.environ.get(name))
    if not present:
        return
    missing = tuple(name for name in _INJECTED_ROOT_ENV_VARS if not os.environ.get(name))
    if missing:
        raise BootstrapError(
            f"{', '.join(present)} set but {', '.join(missing)} missing. "
            "The Electron shell injects all three together; a partial set would split this tier's files "
            "across two layouts. Set all of them or none."
        )


def _check_injected_roots_are_for(root_name: str) -> None:
    """Reject an injected root set when a different tier's directory is the one asked for.

    The injected roots are already qualified for the tier the injecting parent was launched as, so answering another tier with them would hand back the active tier's directory under that tier's name -- silently merging the two. Falling back to the platform default for the odd tier out would be the other half of the same split, so the mismatch has to raise.
    """
    active_root_name = resolve_minds_root_name()
    if root_name == active_root_name:
        return
    raise BootstrapError(
        f"{', '.join(_INJECTED_ROOT_ENV_VARS)} are set for {MINDS_ROOT_NAME_ENV_VAR}={active_root_name!r}, "
        f"but {root_name!r} was asked for. Injected roots are already tier-qualified, so they cannot answer "
        "for another tier."
    )


def _apple_root_for_role(role: MindsPathRole) -> Path:
    """Return the Apple-canonical root for a role, e.g. ``~/Library/Caches/Imbue Studio``.

    Written out rather than read from ``platformdirs``, whose macOS class layers ``$XDG_DATA_HOME`` / ``$XDG_CACHE_HOME`` over these paths (and has no XDG counterpart for the log dir). ``electron/platform-roots.js`` names the same three segments, so on a host that exports those variables the library would move the backend's state and cache off the roots the shell still writes to, and leave logs behind on a third.
    """
    library = Path.home() / "Library"
    match role:
        case MindsPathRole.STATE:
            return library / "Application Support" / MINDS_APP_NAME
        case MindsPathRole.CACHE:
            return library / "Caches" / MINDS_APP_NAME
        case MindsPathRole.LOGS:
            return library / "Logs" / MINDS_APP_NAME
        case _ as unreachable:
            assert_never(unreachable)


def _dotfolder_root_for_role(role: MindsPathRole, root_name: str) -> Path:
    """Return the dotfolder root for a role, e.g. ``~/.minds`` for state and cache and ``~/.minds/logs`` for logs.

    The layout every platform but macOS keeps: one dotfolder per tier holding all three roles, with the tier in the dotfolder's own name rather than a segment below it.
    """
    dotfolder = minds_data_dir_for(root_name)
    match role:
        case MindsPathRole.STATE | MindsPathRole.CACHE:
            return dotfolder
        case MindsPathRole.LOGS:
            return dotfolder / _DOTFOLDER_LOGS_SUBDIR
        case _ as unreachable:
            assert_never(unreachable)


def minds_dir_for_role(role: MindsPathRole, root_name: str, platform: str = sys.platform) -> Path:
    """Return one platform-canonical root for a tier, e.g. ``~/Library/Caches/Imbue Studio/production``.

    Precedence: an injected ``MINDS_*_DIR`` root, then the ``MINDS_DATA_HOME`` throwaway-directory override, then the platform default.
    The injected root wins because the shell already applied ``MINDS_DATA_HOME`` when it resolved it, so honoring the override again here could point the backend somewhere the shell is not looking.
    An injected root answers only for the tier this process was launched as; any other ``root_name`` raises while one is set.

    ``platform`` is a parameter rather than a read of ``sys.platform`` so one platform's layout can be asserted from another: darwin's from the Linux CI host that runs these tests, and the non-darwin guard from a darwin developer machine.
    """
    _check_injected_roots_are_complete()
    injected = os.environ.get(_INJECTED_ENV_VAR_BY_ROLE[role])
    if injected:
        _check_injected_roots_are_for(root_name)
        return Path(injected)
    tier = env_name_from_root_name(root_name)
    data_home = os.environ.get(MINDS_DATA_HOME_ENV_VAR)
    if data_home:
        return Path(data_home) / tier / _DATA_HOME_SUBDIR_BY_ROLE[role]
    if platform != _APPLE_PLATFORM:
        return _dotfolder_root_for_role(role, root_name)
    return _apple_root_for_role(role) / tier


def _tier_names_under(parent: Path) -> tuple[str, ...]:
    """Return the names of ``parent``'s immediate subdirectories, which are the tier names under the Apple and ``MINDS_DATA_HOME`` layouts."""
    if not parent.is_dir():
        return ()
    return tuple(child.name for child in parent.iterdir() if child.is_dir())


def _dotfolder_tier_names() -> tuple[str, ...]:
    """Return the tier of every ``~/.minds`` / ``~/.minds-<tier>`` directory in the home directory.

    A dotfolder whose name is not a root name -- ``~/.mindsomething``, or a hand-made ``~/.minds-backup-copy`` -- contributes nothing, so a name that is not a tier never reaches the caller.
    """
    home = Path.home()
    if not home.is_dir():
        return ()
    tier_names = []
    for child in home.iterdir():
        if not child.is_dir():
            continue
        root_name = child.name.removeprefix(".")
        if root_name != DEFAULT_MINDS_ROOT_NAME and not root_name.startswith(f"{_MINDS_PREFIX}-"):
            continue
        tier_names.append(env_name_from_root_name(root_name))
    return tuple(tier_names)


def list_state_tier_names(platform: str = sys.platform) -> tuple[str, ...]:
    """Return the tier of every state directory on disk, unordered, e.g. ``("production", "staging")``.

    Reads whichever directory the layout keeps its tiers in, because the two nest the tier and the role in opposite orders: the Apple layout and ``MINDS_DATA_HOME`` both give tiers a shared parent, while a dotfolder carries its tier in its own name.
    Filtering these down to legal env names is the caller's job; this only reports what is on disk.
    Injected roots are resolved for one tier only, so they cannot answer for the set of tiers and raise instead.
    """
    _check_injected_roots_are_complete()
    injected = os.environ.get(MINDS_STATE_DIR_ENV_VAR)
    if injected:
        raise BootstrapError(
            f"{MINDS_STATE_DIR_ENV_VAR}={injected!r} is resolved for one tier, so it cannot name where every "
            "tier's state lives. Enumerate tiers from a process that was not handed injected roots."
        )
    data_home = os.environ.get(MINDS_DATA_HOME_ENV_VAR)
    if data_home:
        return _tier_names_under(Path(data_home))
    if platform == _APPLE_PLATFORM:
        return _tier_names_under(_apple_root_for_role(MindsPathRole.STATE))
    return _dotfolder_tier_names()


def resolve_effective_mngr_host_dir() -> Path:
    """The mngr host dir this process should use: the ``MNGR_HOST_DIR`` env override, else mngr's default ``~/.mngr``."""
    mngr_host_dir_str = os.environ.get("MNGR_HOST_DIR")
    return Path(mngr_host_dir_str).expanduser() if mngr_host_dir_str else Path.home() / ".mngr"


class MindsRoot:
    """The resolved identity of the active minds env: the root name and its derived paths.

    A plain immutable class (not pydantic) so the pre-mngr-import layers can construct and pass it.
    Resolve once per process entry point and pass explicitly to the ``mngr_settings`` functions.
    """

    def __init__(self, root_name: str) -> None:
        self._root_name = root_name

    @classmethod
    def from_environment(cls) -> "MindsRoot":
        return cls(resolve_minds_root_name())

    @property
    def root_name(self) -> str:
        return self._root_name

    @property
    def tier(self) -> str:
        return env_name_from_root_name(self._root_name)

    @property
    def legacy_data_dir(self) -> Path:
        """The pre-migration ``~/.<root_name>`` root, read by the migration and nothing else."""
        return minds_data_dir_for(self._root_name)

    @property
    def state_dir(self) -> Path:
        """Secrets, sessions, agent records, the virtualenv -- anything whose loss costs the user something."""
        return minds_dir_for_role(MindsPathRole.STATE, self._root_name)

    @property
    def cache_dir(self) -> Path:
        """Regenerable files the OS is free to delete at any time.

        The virtualenv and the downloaded interpreter do NOT belong here: they are regenerable but the backend cannot boot without them, so a low-disk purge would brick the app.
        """
        return minds_dir_for_role(MindsPathRole.CACHE, self._root_name)

    @property
    def log_dir(self) -> Path:
        return minds_dir_for_role(MindsPathRole.LOGS, self._root_name)

    @property
    def mngr_host_dir(self) -> Path:
        return mngr_host_dir_for(self._root_name)

    @property
    def mngr_prefix(self) -> str:
        return mngr_prefix_for(self._root_name)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, MindsRoot) and other._root_name == self._root_name

    def __hash__(self) -> int:
        return hash(self._root_name)

    def __repr__(self) -> str:
        return f"MindsRoot({self._root_name!r})"


def apply_bootstrap() -> None:
    """Set MNGR_HOST_DIR and MNGR_PREFIX in os.environ from MINDS_ROOT_NAME.

    Must be called before any ``imbue.mngr.*`` module is imported.
    When ``MINDS_ROOT_NAME`` is set to a valid value, the derived ``MNGR_HOST_DIR`` / ``MNGR_PREFIX`` values unconditionally override any pre-existing values -- otherwise an inherited ``MNGR_HOST_DIR`` from a parent process (e.g. a Claude Code agent's tmux env) would silently win and Imbue Studio would read a different mngr settings.toml than the bootstrap wrote to.

    When ``MINDS_ROOT_NAME`` is unset, leaves ``MNGR_HOST_DIR`` / ``MNGR_PREFIX`` untouched -- naming a non-production env is an explicit step, so an unactivated shell has nothing to seed.
    Production-only entry points (the bundled Electron build, and the ``minds`` CLI via :func:`default_root_name_to_production`) always set ``MINDS_ROOT_NAME`` before invoking us, so an unset value here genuinely means "the user has not activated any env yet" -- which is what ``minds-admin`` relies on.

    When ``MINDS_ROOT_NAME`` is set to an invalid value, raises ``BootstrapError`` (via :func:`resolve_minds_root_name`); ``main.py`` turns that into a clean one-line error.

    The companion settings reconciliation (which must also precede any mngr import) lives in :func:`imbue.minds.mngr_settings.reconcile.ensure_mngr_settings_before_mngr_import`.
    """
    raw_value = os.environ.get(MINDS_ROOT_NAME_ENV_VAR)
    if raw_value is None:
        return
    root_name = resolve_minds_root_name()
    os.environ["MNGR_HOST_DIR"] = str(mngr_host_dir_for(root_name))
    os.environ["MNGR_PREFIX"] = mngr_prefix_for(root_name)
