import subprocess
import sys
from pathlib import Path

from scripts.changelog_consolidation_trigger import PROVIDER
from scripts.changelog_consolidation_trigger import _ENABLED_PLUGINS
from scripts.changelog_consolidation_trigger import disable_plugin_args

_SCRIPT_PATH = Path(__file__).resolve().parent / "changelog_consolidation_trigger.py"


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
    """The CLI flag is the integration point with changelog_deploy.sh
    (which reads it to set $PROVIDER); its output must match the in-process
    constant so the deploy and release.py's printed on-demand command can't
    drift.
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
