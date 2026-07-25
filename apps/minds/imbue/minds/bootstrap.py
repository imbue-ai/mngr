"""Translate MINDS_ROOT_NAME into MNGR_HOST_DIR and MNGR_PREFIX.

This must run before any ``imbue.mngr.*`` module is imported, because mngr reads ``MNGR_HOST_DIR`` and ``MNGR_PREFIX`` during its own module-level initialization (plugin manager construction, config discovery, etc.).

Kept intentionally minimal -- stdlib only -- so it stays cheap to import and cannot accidentally pull in mngr before translation happens.
The settings-file machinery that shares this constraint lives in :mod:`imbue.minds.mngr_settings`.
"""

import os
import re
from pathlib import Path
from typing import Final

MINDS_ROOT_NAME_ENV_VAR: Final[str] = "MINDS_ROOT_NAME"
DEFAULT_MINDS_ROOT_NAME: Final[str] = "minds"
_MINDS_PREFIX: Final[str] = "minds"
# Legal env-name suffixes after ``minds-``.
# Mirrors the rules in :mod:`imbue.minds.envs.primitives` and the reserved tier names in :mod:`imbue.minds.cli.env`:
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


class BootstrapError(ValueError):
    """Raised when the minds bootstrap layer can't compute a derived value.

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
            f'Run `unset {MINDS_ROOT_NAME_ENV_VAR}`, then `eval "$(minds env activate <name>)"` '
            "to activate a valid env."
        )
    return value


def is_env_activated() -> bool:
    """Return whether ``MINDS_ROOT_NAME`` is set in the environment.

    Used by ``minds env deploy/destroy`` and ``minds run`` to refuse when no env has been activated; ``MINDS_ROOT_NAME=minds`` counts as an explicit activation of production.
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
    """Return the minds data directory for a given root name (e.g. ~/.minds)."""
    return Path.home() / ".{}".format(root_name)


def mngr_host_dir_for(root_name: str) -> Path:
    """Return the mngr host directory for a given root name (e.g. ~/.minds/mngr)."""
    return minds_data_dir_for(root_name) / "mngr"


def mngr_prefix_for(root_name: str) -> str:
    """Return the mngr prefix for a given root name (e.g. minds-)."""
    return "{}-".format(root_name)


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
    def data_dir(self) -> Path:
        return minds_data_dir_for(self._root_name)

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
    When ``MINDS_ROOT_NAME`` is set to a valid value, the derived ``MNGR_HOST_DIR`` / ``MNGR_PREFIX`` values unconditionally override any pre-existing values -- otherwise an inherited ``MNGR_HOST_DIR`` from a parent process (e.g. a Claude Code agent's tmux env) would silently win and minds would read a different mngr settings.toml than the bootstrap wrote to.

    When ``MINDS_ROOT_NAME`` is unset, leaves ``MNGR_HOST_DIR`` / ``MNGR_PREFIX`` untouched -- env activation is an explicit ``minds env activate`` step, so an unactivated shell has nothing to seed.
    Production-only entry points (the bundled Electron build) always set both ``MINDS_ROOT_NAME`` and the derived vars before invoking us, so an unset value here genuinely means "the user has not activated any env yet".

    When ``MINDS_ROOT_NAME`` is set to an invalid value, raises ``BootstrapError`` (via :func:`resolve_minds_root_name`); ``main.py`` turns that into a clean one-line error.

    The companion settings reconciliation (which must also precede any mngr import) lives in :func:`imbue.minds.mngr_settings.reconcile.ensure_mngr_settings_before_mngr_import`.
    """
    raw_value = os.environ.get(MINDS_ROOT_NAME_ENV_VAR)
    if raw_value is None:
        return
    root_name = resolve_minds_root_name()
    os.environ["MNGR_HOST_DIR"] = str(mngr_host_dir_for(root_name))
    os.environ["MNGR_PREFIX"] = mngr_prefix_for(root_name)
