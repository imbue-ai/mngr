"""Filesystem paths for the per-env data root layout.

Every minds env owns one data root: ``~/.minds/`` for production,
``~/.minds-<env-name>/`` for every other env. Activation
(``minds env activate <name>``) exports ``MINDS_ROOT_NAME`` /
``MNGR_HOST_DIR`` / ``MNGR_PREFIX`` / ``MINDS_CLIENT_CONFIG_PATH`` so
the rest of the stack picks up the right root without per-call
plumbing.

Per-env on-disk state is split into two files under the env root:

* ``client.toml`` -- non-secret config (connector URL, LiteLLM proxy
  URL). For dev envs, written by ``minds env deploy``. For staging /
  production, the same shape lives in-repo at
  ``apps/minds/imbue/minds/config/envs/<tier>/client.toml`` (committed
  to the repo) and ``minds env activate`` points
  ``MINDS_CLIENT_CONFIG_PATH`` at that path instead.
* ``secrets.toml`` -- chmod-0600 dev-env-only file holding the values
  ``minds env deploy`` generated (Neon DSN, SuperTokens connection URI,
  SuperTokens API key). Staging / production fetch those values from
  Vault at deploy time instead, so they never have a local secrets file.
"""

import os
from pathlib import Path

from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import env_name_from_root_name
from imbue.minds.bootstrap import is_minds_root_name_set_to_active_env
from imbue.minds.bootstrap import minds_data_dir_for
from imbue.minds.bootstrap import resolve_minds_root_name
from imbue.minds.bootstrap import root_name_for_env_name
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.primitives import InvalidDevEnvNameError

_CLIENT_FILENAME = "client.toml"
_SECRETS_FILENAME = "secrets.toml"
_MINDS_PREFIX = "minds"


def env_root_dir(name: DevEnvName) -> Path:
    """Return ``~/.minds-<name>/`` (or ``~/.minds/`` for ``production``).

    Computed via :func:`root_name_for_env_name` so the special-cased
    ``production`` -> ``~/.minds/`` mapping stays in one place.
    """
    return minds_data_dir_for(root_name_for_env_name(str(name)))


def client_config_file(name: DevEnvName) -> Path:
    """Return ``~/.minds-<name>/client.toml`` -- the non-secret per-env config path.

    For ``staging`` / ``production`` the source of truth is the in-repo
    file (see :func:`imbue.minds.config.loader.repo_tier_client_config_path`);
    this function returns the under-root path regardless, because that's
    where the activation flow lays down a copy when needed (it is not
    written for staging / production, but the path is the canonical
    answer to "where would a per-env client.toml live for this env?").
    """
    return env_root_dir(name) / _CLIENT_FILENAME


def secrets_file(name: DevEnvName) -> Path:
    """Return ``~/.minds-<name>/secrets.toml`` -- the chmod-0600 dev-env secrets path.

    Only ever written for dev envs; staging / production fetch the same
    values from Vault at deploy time.
    """
    return env_root_dir(name) / _SECRETS_FILENAME


def list_env_root_dirs() -> tuple[Path, ...]:
    """Glob the user's home for every ``~/.minds*/`` directory.

    Returns each existing root in sorted order, with ``~/.minds/``
    (production) first if it exists. Used by ``minds env list`` to
    enumerate every env on disk -- including ones the user manually
    ``mkdir``'d. Callers that need to filter by "has a real
    ``client.toml``" do so themselves.
    """
    home = Path.home()
    if not home.is_dir():
        return ()
    matches: list[Path] = []
    for child in home.iterdir():
        if not child.is_dir():
            continue
        # ``~/.minds`` (production) and ``~/.minds-<name>`` (everything
        # else). Anything else under ``~`` whose name happens to start
        # with ``.minds`` (e.g. ``~/.minds-backup-2024-01-01``) is left
        # out -- the env-name regex forbids both an empty suffix after
        # the hyphen and any non-suffix continuation.
        if child.name == f".{_MINDS_PREFIX}":
            matches.append(child)
            continue
        if not child.name.startswith(f".{_MINDS_PREFIX}-"):
            continue
        env_name = child.name[len(f".{_MINDS_PREFIX}-") :]
        if not _is_legal_env_name(env_name):
            continue
        matches.append(child)
    return tuple(sorted(matches, key=_env_root_sort_key))


def _env_root_sort_key(path: Path) -> tuple[int, str]:
    # ``~/.minds`` (production) sorts first, then everything else
    # alphabetically by env name. The numeric prefix keeps production
    # at the head of the list even when its dirname (``.minds``) would
    # otherwise sort between hypothetical ``.mindd*`` / ``.minde*``
    # neighbors.
    if path.name == f".{_MINDS_PREFIX}":
        return (0, "")
    return (1, path.name)


def _is_legal_env_name(env_name: str) -> bool:
    """Return True iff ``env_name`` matches the DevEnvName regex.

    Inlined check instead of constructing :class:`DevEnvName` so the
    glob can scan ``~`` without raising on every unrelated directory.
    """
    if not env_name:
        return False
    try:
        DevEnvName(env_name)
    except InvalidDevEnvNameError:
        return False
    return True


def active_env_name_or_none() -> str | None:
    """Return the env name implied by ``MINDS_ROOT_NAME``, or None.

    Returns ``production`` for ``MINDS_ROOT_NAME=minds``, the env name
    for ``MINDS_ROOT_NAME=minds-<env>``, and ``None`` for unset or
    invalid values (i.e. the caller has not activated any env). Used
    by ``minds env deploy`` / ``destroy`` to refuse without explicit
    activation.
    """
    if not is_minds_root_name_set_to_active_env():
        return None
    return env_name_from_root_name(os.environ[MINDS_ROOT_NAME_ENV_VAR])


def resolved_env_root_dir() -> Path:
    """Return the ``minds_data_dir_for`` of the resolved root name.

    Used by callers that just want "where does my mngr profile / auth /
    agents live" without caring whether the user has activated a real
    env. Falls back to ``~/.minds/`` when nothing is activated.
    """
    return minds_data_dir_for(resolve_minds_root_name())
