"""Shared identifiers + plugin-disable args for the changelog-consolidation schedule.

Three consumers need to agree on how to address this schedule:
``changelog_deploy.sh`` (which deploys it), the ``changelog-trigger``
justfile recipe (which runs it on demand), and ``scripts/release.py``
(whose pre-release gate names it when blocking). The two shell
consumers must share the same ``--disable-plugin <name>`` list passed to
``mngr schedule …`` calls, the schedule's deployed provider, and the
trigger / namespace identifiers. This module is the source of truth.

The shell consumers (the deploy script and the justfile recipe) read
the provider and disable list through this module's CLI:
``--print-disable-plugin-args`` for the disable list and
``--print-provider`` for the provider. ``scripts/release.py`` imports
only ``TRIGGER_NAME`` directly -- enough to name the schedule in its
gate's error message; it points users at ``just changelog-trigger``
rather than constructing the ``mngr schedule run`` command itself.
``TRIGGER_NAME`` and ``MNGR_ROOT_NAME`` are still duplicated as bash
literals in the two shell consumers because they need the values before
any ``uv run python`` invocation; those literals must be kept in sync
with the constants here by hand.

This module also exposes ``--stop-all-apps``, which ``changelog_deploy.sh``
runs before redeploying to stop *every* Modal app in the schedule's
isolated environment(s). The schedule lives in its own environment (named
``{MNGR_ROOT_NAME}-<user_id>``), so stopping all apps there is a safe,
naming-scheme-independent guarantee that no orphaned cron app survives a
redeploy -- a past app-naming-scheme change had left an orphan firing a
second nightly run because ``mngr schedule remove`` only stops the app
matching the *current* name.
"""

import argparse
import importlib.metadata
import json
import subprocess
import sys
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final

TRIGGER_NAME: Final[str] = "changelog-consolidation"
MNGR_ROOT_NAME: Final[str] = "mngr-changelog-schedule"
# The schedule is deployed against this provider; the changelog-trigger
# justfile recipe reads this value (via --print-provider) so its
# on-demand run targets the deployment that actually exists. Editing
# in-source is fine -- (re)deploys are rare and the file edit is the
# deliberate trigger.
PROVIDER: Final[str] = "modal"
# Plugins the schedule trigger needs to function; everything else is
# disabled to avoid loading repo-specific plugins that would error on
# import in this isolated mngr config namespace.
_ENABLED_PLUGINS: Final[frozenset[str]] = frozenset({"schedule", "modal", "headless_claude", "claude", "file"})


def disable_plugin_args() -> list[str]:
    """Compute ``--disable-plugin <name>`` args for ``mngr schedule`` invocations.

    The mngr CLI auto-loads every plugin registered in the ``mngr``
    entry-point group. Many of this repo's plugins reference code that
    isn't importable from the isolated config namespace the consolidation
    schedule uses, so we disable everything except the minimum set the
    schedule needs.
    """
    installed = {ep.name for ep in importlib.metadata.entry_points(group="mngr")}
    to_disable = sorted(installed - _ENABLED_PLUGINS)
    args: list[str] = []
    for name in to_disable:
        args.extend(["--disable-plugin", name])
    return args


# Keys emitted by ``modal environment list --json`` and ``modal app list
# --json`` (the column headers from the Modal CLI's table output, which are
# the JSON keys verbatim). The app keys match the working callers in
# libs/mngr_schedule (testing.py, implementations/modal/deploy.py) and
# scripts/modal_nuke.py. The environment-name column casing has shifted across
# Modal versions ("Name" in 1.4.x, "name" in older builds -- see
# apps/minds/imbue/minds/deployment_tests/helpers.py), so we accept either.
_ENV_NAME_KEYS: Final[tuple[str, ...]] = ("Name", "name")
_APP_ID_KEY: Final[str] = "App ID"
_APP_STATE_KEY: Final[str] = "State"
# App states (lowercased) that mean the app is already not running, so there
# is nothing to stop.
_ALREADY_STOPPED_STATES: Final[frozenset[str]] = frozenset({"stopped", "stopping"})

# A callable that runs ``modal <args>`` and returns the completed process.
# Injected so the sweep can be unit-tested without invoking the real CLI.
ModalRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class ModalCommandError(Exception):
    """A ``modal`` CLI invocation we depend on failed."""


class ModalSchemaError(Exception):
    """``modal ... --json`` output is missing an expected key.

    Stopping apps is destructive, so we fail loudly (rather than act on a
    placeholder identifier) if the Modal CLI changes its ``--json`` schema.
    """


def _run_modal(args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["modal", *args], capture_output=True, text=True, timeout=60)


def _require_key(entry: Mapping[str, object], key: str, kind: str) -> str:
    if key not in entry:
        raise ModalSchemaError(
            f"Modal {kind} entry is missing expected key {key!r}; got keys {sorted(entry)!r}. "
            "Refusing to act on an unknown identifier; the modal --json schema may have changed."
        )
    return str(entry[key])


def _require_first_key(entry: Mapping[str, object], keys: Sequence[str], kind: str) -> str:
    """Return the value of the first of ``keys`` present in ``entry``.

    Tolerates Modal's column-casing drift across CLI versions while still
    failing loudly (rather than acting on a placeholder identifier) if none of
    the expected keys are present.
    """
    for key in keys:
        if key in entry:
            return str(entry[key])
    raise ModalSchemaError(
        f"Modal {kind} entry is missing all expected keys {list(keys)!r}; got keys {sorted(entry)!r}. "
        "Refusing to act on an unknown identifier; the modal --json schema may have changed."
    )


def _changelog_environment_names(run_modal: ModalRunner) -> list[str]:
    """Return the names of the changelog schedule's isolated Modal environment(s).

    The schedule's environment is named ``{MNGR_ROOT_NAME}-<user_id>``, so any
    environment whose name starts with ``MNGR_ROOT_NAME`` belongs to this
    schedule (the root name is unique to it).
    """
    result = run_modal(["environment", "list", "--json"])
    if result.returncode != 0:
        raise ModalCommandError(f"`modal environment list` failed: {result.stderr.strip()}")
    environments = json.loads(result.stdout)
    names = [_require_first_key(env, _ENV_NAME_KEYS, "environment") for env in environments]
    return sorted(name for name in names if name.startswith(MNGR_ROOT_NAME))


def stop_all_apps_in_changelog_envs(
    run_modal: ModalRunner = _run_modal,
    *,
    is_dry_run: bool = False,
) -> list[tuple[str, str]]:
    """Stop every running Modal app in the changelog schedule's environment(s).

    This is the orphan-proof complement to ``mngr schedule remove`` (which only
    stops the app whose name matches the *current* naming scheme). Because the
    schedule has its own dedicated environment, stopping all apps there is safe.

    Returns the ``(environment, app_id)`` pairs that were stopped (in dry-run,
    the pairs that *would* be stopped). A failure to stop an individual app is
    logged and skipped rather than aborting the redeploy.
    """
    stopped: list[tuple[str, str]] = []
    for env_name in _changelog_environment_names(run_modal):
        list_result = run_modal(["app", "list", "--json", "-e", env_name])
        if list_result.returncode != 0:
            raise ModalCommandError(f"`modal app list` failed for env {env_name!r}: {list_result.stderr.strip()}")
        for app in json.loads(list_result.stdout):
            if _require_key(app, _APP_STATE_KEY, "app").lower() in _ALREADY_STOPPED_STATES:
                continue
            app_id = _require_key(app, _APP_ID_KEY, "app")
            if is_dry_run:
                print(f"[dry-run] would stop Modal app {app_id} in env {env_name}", file=sys.stderr)
                stopped.append((env_name, app_id))
                continue
            stop_result = run_modal(["app", "stop", app_id, "-e", env_name, "--yes"])
            if stop_result.returncode == 0:
                print(f"Stopped Modal app {app_id} in env {env_name}", file=sys.stderr)
                stopped.append((env_name, app_id))
            else:
                print(
                    f"WARNING: failed to stop Modal app {app_id} in env {env_name}: {stop_result.stderr.strip()}",
                    file=sys.stderr,
                )
    return stopped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--print-disable-plugin-args",
        action="store_true",
        help="Print the --disable-plugin args (space-separated) and exit. "
        "Used by changelog_deploy.sh and the changelog-trigger justfile recipe so they stay in sync.",
    )
    parser.add_argument(
        "--print-provider",
        action="store_true",
        help="Print the deployed provider name and exit. "
        "Used by changelog_deploy.sh and the changelog-trigger justfile recipe.",
    )
    parser.add_argument(
        "--stop-all-apps",
        action="store_true",
        help="Stop every Modal app in the changelog schedule's isolated environment(s). "
        "Used by changelog_deploy.sh before redeploy to clear orphaned cron apps.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --stop-all-apps, only print which apps would be stopped.",
    )
    args = parser.parse_args()
    if args.print_disable_plugin_args:
        print(" ".join(disable_plugin_args()))
        return
    if args.print_provider:
        print(PROVIDER)
        return
    if args.stop_all_apps:
        stopped = stop_all_apps_in_changelog_envs(is_dry_run=args.dry_run)
        verb = "Would stop" if args.dry_run else "Stopped"
        print(f"{verb} {len(stopped)} Modal app(s) in the changelog environment(s).", file=sys.stderr)
        return
    # parser.error() prints usage to stderr and calls sys.exit(2); it does not return.
    parser.error("no action specified; pass --print-disable-plugin-args, --print-provider, or --stop-all-apps")


if __name__ == "__main__":
    main()
