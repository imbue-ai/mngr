import os
import shlex
import subprocess

from imbue.minds.desktop_client.in_workspace_mngr import FAILURE_DETAIL_MAX_CHARS
from imbue.minds.desktop_client.in_workspace_mngr import build_in_workspace_mngr_command
from imbue.minds.desktop_client.in_workspace_mngr import in_workspace_failure_detail

# The shape a wedged workspace answers with: outer discovery chatter, the
# in-container mngr's refusal, and the outer exec's own closing verdict.
_UNPARSEABLE_CONFIG_STDERR = """WARNING: imbue_cloud[gabriel] outer SSH unreachable for host host-abc: Host not found: host-abc
WARNING: Duplicate host name 'workspace-1' found on provider 'imbue_cloud_gabriel'
Error: Unknown fields in agent_types.opencode: ['append_system_prompt', 'auto_allow_permissions']. \
Valid fields: ['cli_args', 'command']
If 'opencode' is provided by a disabled plugin, enable it.
ERROR: Command failed on agent system-services
"""


# What a workspace answers when the shell resolved a stale second copy of mngr:
# the inner command dies at import, with no verdict of its own.
_STALE_MNGR_IMPORT_STDERR = """WARNING: imbue_cloud[gabriel] outer SSH unreachable for host host-abc: Host not found: host-abc
Traceback (most recent call last):
  File "/home/user/.local/bin/mngr", line 4, in <module>
  File "/mngr-vol/home/workspace/system/vendor/mngr/libs/mngr/imbue/mngr/utils/file_watch.py", line 18, in <module>
ModuleNotFoundError: No module named 'watchdog'
ERROR: Command failed on agent system-services
"""


def test_the_built_command_runs_mngr_with_unknown_config_tolerated() -> None:
    command = build_in_workspace_mngr_command(["list", "--format", "{name}"])
    # Shell env-assignment prefixes: read before any config parse, unlike a
    # --setting, which is itself config.
    assert command.startswith("PATH=/root/.local/bin:$PATH MNGR_ALLOW_UNKNOWN_CONFIG=1 mngr list ")
    assert shlex.split(command) == [
        "PATH=/root/.local/bin:$PATH",
        "MNGR_ALLOW_UNKNOWN_CONFIG=1",
        "mngr",
        "list",
        "--format",
        "{name}",
    ]


def test_the_image_tool_dir_leads_the_path_the_process_actually_sees() -> None:
    """A login shell that prepended a stale ``$HOME/.local/bin`` must still resolve the image's copy first."""
    command = build_in_workspace_mngr_command(["printenv", "PATH"])
    script = command.replace("mngr printenv", "env printenv", 1)
    shadowing_path = f"/home/user/.local/bin:{os.environ['PATH']}"
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, env={"PATH": shadowing_path})

    assert result.stdout.strip() == f"/root/.local/bin:{shadowing_path}"


def test_the_tolerance_reaches_the_process_the_shell_actually_runs() -> None:
    """The prefix must survive the shell ``mngr exec`` runs the command through, not just look right."""
    command = build_in_workspace_mngr_command(["printenv", "MNGR_ALLOW_UNKNOWN_CONFIG"])
    # Stand a `mngr` in for the container's: the prefix is what is under test.
    script = command.replace("mngr printenv", "env printenv", 1)
    # ``sh``, not bash: that is what mngr exec runs a COMMAND through, and a
    # container's /bin/sh need not be bash.
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True)

    assert result.stdout.strip() == "1"


def test_an_argument_cannot_break_out_of_the_command() -> None:
    hostile = 'oops"; rm -rf /; echo $(whoami) `id` && touch /tmp/pwned\n\nsecond line'
    command = build_in_workspace_mngr_command(["create", "--message", hostile])

    assert shlex.split(command)[-2:] == ["--message", hostile]


def test_the_detail_is_the_inner_verdict_not_the_outer_wrapper() -> None:
    """The last verdict is the exec's own "command failed", which says nothing about why."""
    detail = in_workspace_failure_detail(_UNPARSEABLE_CONFIG_STDERR)

    assert detail.startswith("Error: Unknown fields in agent_types.opencode")
    # The hint that follows the verdict is part of the diagnosis and must survive.
    assert "provided by a disabled plugin" in detail


def test_the_detail_drops_the_outer_chatter_that_reads_as_the_cause() -> None:
    """An unreachable *other* host is what the user would otherwise blame for the refusal."""
    detail = in_workspace_failure_detail(_UNPARSEABLE_CONFIG_STDERR)

    assert "outer SSH unreachable" not in detail
    assert "Duplicate host name" not in detail


def test_a_verdictless_inner_death_is_diagnosed_by_its_last_line() -> None:
    """The wrapper's "Command failed on agent" is the only marker, and it names no cause."""
    detail = in_workspace_failure_detail(_STALE_MNGR_IMPORT_STDERR)

    assert detail == "ModuleNotFoundError: No module named 'watchdog'"


def test_an_unmarked_failure_still_yields_the_tail_it_has() -> None:
    detail = in_workspace_failure_detail("ssh: connect to host 10.0.0.1 port 22: Connection refused\n")

    assert detail == "ssh: connect to host 10.0.0.1 port 22: Connection refused"


def test_a_log_dump_is_bounded_before_it_reaches_the_user() -> None:
    detail = in_workspace_failure_detail("Error: it broke\n" + "noise\n" * 5000)

    assert len(detail) == FAILURE_DETAIL_MAX_CHARS
    assert detail.startswith("Error: it broke")
