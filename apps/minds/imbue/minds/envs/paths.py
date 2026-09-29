"""Filesystem paths for the per-env data root layout.

Every minds env owns one state root, the tier directory under
``~/Library/Application Support/Imbue Studio/`` (``production`` for production,
the env name for everything else). Activation
(``minds-admin env activate <name>``) exports ``MINDS_ROOT_NAME`` /
``MNGR_HOST_DIR`` / ``MNGR_PREFIX`` / ``MINDS_CLIENT_CONFIG_PATH`` so
the rest of the stack picks up the right root without per-call
plumbing.

Per-env on-disk state is split into two files under the env root:

* ``client.toml`` -- non-secret config (connector URL, LiteLLM proxy
  URL). For dev envs, written by ``minds-admin env deploy``. For staging /
  production, the same shape lives in-repo at
  ``apps/minds/imbue/minds/config/envs/<tier>/client.toml`` (committed
  to the repo) and ``minds-admin env activate`` points
  ``MINDS_CLIENT_CONFIG_PATH`` at that path instead.
* ``secrets.toml`` -- chmod-0600 dev-env-only file holding the values
  ``minds-admin env deploy`` generated (Neon DSN, SuperTokens connection URI,
  SuperTokens API key). Staging / production fetch those values from
  Vault at deploy time instead, so they never have a local secrets file.
"""

import os
from pathlib import Path

from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import MindsPathRole
from imbue.minds.bootstrap import env_name_from_root_name
from imbue.minds.bootstrap import is_env_activated
from imbue.minds.bootstrap import list_state_tier_names
from imbue.minds.bootstrap import minds_dir_for_role
from imbue.minds.bootstrap import resolve_minds_root_name
from imbue.minds.bootstrap import root_name_for_env_name
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.primitives import InvalidDevEnvNameError

_CLIENT_FILENAME = "client.toml"
_SECRETS_FILENAME = "secrets.toml"
_PRODUCTION_TIER = "production"


def env_root_dir(name: DevEnvName) -> Path:
    """Return the env's state root, where its ``client.toml`` / ``secrets.toml`` live.

    Computed via :func:`root_name_for_env_name` so the special-cased
    ``production`` mapping stays in one place. This is the state root
    rather than the legacy ``~/.minds-<name>/`` because the first-launch
    migration moves both files there; pointing at the dotfolder would
    make ``minds env`` read a path the shell has already emptied.
    """
    return minds_dir_for_role(MindsPathRole.STATE, root_name_for_env_name(str(name)))


def env_log_dir(name: DevEnvName) -> Path:
    """Return the env's log root, which sits beside the state root rather than inside it.

    macOS excludes ``~/Library/Logs`` from Time Machine, which is the
    whole reason logs are their own root. Callers that clear an env's
    local state need this separately from :func:`env_root_dir`.
    """
    return minds_dir_for_role(MindsPathRole.LOGS, root_name_for_env_name(str(name)))


def client_config_file(name: DevEnvName) -> Path:
    """Return the env's ``client.toml`` -- the non-secret per-env config path.

    For ``staging`` / ``production`` the source of truth is the in-repo
    file (see :func:`imbue.minds.config.loader.repo_tier_client_config_path`);
    this function returns the under-root path regardless, because that's
    where the activation flow lays down a copy when needed (it is not
    written for staging / production, but the path is the canonical
    answer to "where would a per-env client.toml live for this env?").
    """
    return env_root_dir(name) / _CLIENT_FILENAME


def secrets_file(name: DevEnvName) -> Path:
    """Return the env's ``secrets.toml`` -- the chmod-0600 dev-env secrets path.

    Only ever written for dev envs; staging / production fetch the same
    values from Vault at deploy time.
    """
    return env_root_dir(name) / _SECRETS_FILENAME


def list_env_names() -> tuple[str, ...]:
    """Every env whose state root exists on disk, production first.

    Asks :func:`list_state_tier_names` for the tiers the active layout has on
    disk, which on macOS are the subdirectories of the shared state parent
    rather than the ``~/.minds*`` dotfolders: after the first-launch migration
    those are emptied husks, so reading them would list envs whose files have
    all moved. Names rather than paths, because a tier directory is the state
    root only under the Apple layout -- ``MINDS_DATA_HOME`` puts the role below
    the tier -- so :func:`env_root_dir` is what turns one into the other. Used
    by ``minds-admin env list``; callers that need to filter by "has a real
    ``client.toml``" do so themselves.
    """
    matches = [
        tier_name
        for tier_name in list_state_tier_names()
        if (tier_name == _PRODUCTION_TIER or _is_legal_env_name(tier_name))
        and env_root_dir(DevEnvName(tier_name)).is_dir()
    ]
    return tuple(sorted(matches, key=_env_name_sort_key))


def _env_name_sort_key(env_name: str) -> tuple[int, str]:
    # Production sorts first, then everything else alphabetically by env name.
    if env_name == _PRODUCTION_TIER:
        return (0, "")
    return (1, env_name)


def _is_legal_env_name(env_name: str) -> bool:
    """Return True iff ``env_name`` matches the DevEnvName regex.

    A probe rather than a construction, so the tier walk can pass over a
    directory that is not an env (Electron's ``Crashpad``, say) instead of
    raising on it.
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
    by ``minds-admin env deploy`` / ``destroy`` to refuse without explicit
    activation.
    """
    if not is_env_activated():
        return None
    return env_name_from_root_name(os.environ[MINDS_ROOT_NAME_ENV_VAR])


def resolved_env_root_dir() -> Path:
    """Return the state root of the resolved root name.

    Used by callers that just want "where does my mngr profile / auth /
    agents live" without caring whether the user has activated a real
    env. Falls back to production when nothing is activated.
    """
    return minds_dir_for_role(MindsPathRole.STATE, resolve_minds_root_name())
