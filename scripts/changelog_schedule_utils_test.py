import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts.changelog_schedule_utils import MNGR_ROOT_NAME
from scripts.changelog_schedule_utils import ModalCommandError
from scripts.changelog_schedule_utils import ModalSchemaError
from scripts.changelog_schedule_utils import PROVIDER
from scripts.changelog_schedule_utils import _ENABLED_PLUGINS
from scripts.changelog_schedule_utils import disable_plugin_args
from scripts.changelog_schedule_utils import stop_all_apps_in_changelog_envs

_SCRIPT_PATH = Path(__file__).resolve().parent / "changelog_schedule_utils.py"


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=["modal"], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeModal:
    """A fake ``ModalRunner`` that answers ``environment list`` / ``app list`` /
    ``app stop`` from in-memory state and records which apps were stopped.
    """

    def __init__(
        self,
        environments: list[dict[str, str]],
        apps_by_env: dict[str, list[dict[str, str]]],
        stop_failures: frozenset[str] = frozenset(),
    ) -> None:
        self.environments = environments
        self.apps_by_env = apps_by_env
        self.stop_failures = stop_failures
        self.stopped: list[tuple[str, str]] = []

    def __call__(self, args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        args = list(args)
        if args[:2] == ["environment", "list"]:
            return _completed(stdout=json.dumps(self.environments))
        if args[:2] == ["app", "list"]:
            env = args[args.index("-e") + 1]
            return _completed(stdout=json.dumps(self.apps_by_env.get(env, [])))
        if args[:2] == ["app", "stop"]:
            app_id = args[2]
            env = args[args.index("-e") + 1]
            assert "--yes" in args, "app stop must pass --yes to avoid the interactive-terminal abort"
            if app_id in self.stop_failures:
                return _completed(returncode=1, stderr="boom")
            self.stopped.append((env, app_id))
            return _completed()
        raise AssertionError(f"unexpected modal args: {args}")


@pytest.mark.parametrize("env_name_key", ["Name", "name"])
def test_stop_all_apps_stops_running_apps_in_matching_envs_only(env_name_key: str) -> None:
    # Modal's environment-name column casing has shifted across CLI versions
    # ("Name" in 1.4.x, "name" in older builds); both must be handled.
    target_env = f"{MNGR_ROOT_NAME}-aaa"
    fake = FakeModal(
        environments=[{env_name_key: target_env}, {env_name_key: "mngr-other-user-bbb"}],
        apps_by_env={
            target_env: [
                {"App ID": "ap-1", "State": "deployed"},
                {"App ID": "ap-2", "State": "stopped"},
            ],
        },
    )
    result = stop_all_apps_in_changelog_envs(fake)
    # Only the running app in the changelog-prefixed env is stopped; the
    # already-stopped app and the unrelated environment are left alone.
    assert result == [(target_env, "ap-1")]
    assert fake.stopped == [(target_env, "ap-1")]


def test_stop_all_apps_raises_on_missing_environment_name_key() -> None:
    fake = FakeModal(environments=[{"unexpected": "x"}], apps_by_env={})
    with pytest.raises(ModalSchemaError):
        stop_all_apps_in_changelog_envs(fake)


def test_stop_all_apps_dry_run_reports_without_stopping() -> None:
    target_env = f"{MNGR_ROOT_NAME}-aaa"
    fake = FakeModal(
        environments=[{"name": target_env}],
        apps_by_env={target_env: [{"App ID": "ap-1", "State": "deployed"}]},
    )
    result = stop_all_apps_in_changelog_envs(fake, is_dry_run=True)
    assert result == [(target_env, "ap-1")]
    assert fake.stopped == []


def test_stop_all_apps_continues_past_individual_stop_failure() -> None:
    target_env = f"{MNGR_ROOT_NAME}-aaa"
    fake = FakeModal(
        environments=[{"name": target_env}],
        apps_by_env={
            target_env: [
                {"App ID": "ap-1", "State": "deployed"},
                {"App ID": "ap-2", "State": "running"},
            ]
        },
        stop_failures=frozenset({"ap-1"}),
    )
    result = stop_all_apps_in_changelog_envs(fake)
    # ap-1's stop fails (omitted from the result); the sweep still stops ap-2.
    assert result == [(target_env, "ap-2")]


def test_stop_all_apps_raises_on_missing_app_id_key() -> None:
    target_env = f"{MNGR_ROOT_NAME}-aaa"
    fake = FakeModal(
        environments=[{"name": target_env}],
        apps_by_env={target_env: [{"State": "deployed"}]},
    )
    with pytest.raises(ModalSchemaError):
        stop_all_apps_in_changelog_envs(fake)


def test_stop_all_apps_raises_on_environment_list_failure() -> None:
    def failing(args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        return _completed(returncode=1, stderr="not logged in")

    with pytest.raises(ModalCommandError):
        stop_all_apps_in_changelog_envs(failing)


def test_disable_plugin_args_returns_paired_flags() -> None:
    args = disable_plugin_args()
    # args should be a list of (--disable-plugin, NAME) pairs.
    assert len(args) % 2 == 0
    for i in range(0, len(args), 2):
        assert args[i] == "--disable-plugin"
        assert args[i + 1] != ""
    names = args[1::2]
    # The minimum-required plugins must never be disabled.
    assert _ENABLED_PLUGINS.isdisjoint(names)
    # Names should be unique (no double-disables).
    assert len(names) == len(set(names))


def test_cli_print_disable_plugin_args_matches_helper() -> None:
    """The CLI flag is the integration point with changelog_deploy.sh;
    its output must match the in-process helper.
    """
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--print-disable-plugin-args"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert result.stdout.strip() == " ".join(disable_plugin_args())


def test_cli_print_provider_matches_constant() -> None:
    """The CLI flag is the integration point with changelog_deploy.sh and the
    changelog-trigger justfile recipe (both read it to set the provider); its
    output must match the in-process constant so the deploy and the on-demand
    trigger can't drift.
    """
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--print-provider"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert result.stdout.strip() == PROVIDER


def test_cli_without_action_errors_and_mentions_flag() -> None:
    """No action -> parser.error: exit code 2 and the flag name appears in stderr
    so the user knows what to pass.
    """
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 2
    assert "--print-disable-plugin-args" in result.stderr
