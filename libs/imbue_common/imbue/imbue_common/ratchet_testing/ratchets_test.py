import subprocess
import textwrap
from pathlib import Path

from imbue.imbue_common.ratchet_testing.ratchets import find_if_elif_without_else
from imbue.imbue_common.ratchet_testing.ratchets import parse_type_check_output


def _commit_module(git_repo: Path, source: str) -> None:
    (git_repo / "module.py").write_text(textwrap.dedent(source).lstrip("\n"))
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add module"], cwd=git_repo, check=True, capture_output=True)


def test_finds_elif_chain_without_else(git_repo: Path) -> None:
    _commit_module(
        git_repo,
        """
        def f(x: int) -> str:
            if x == 1:
                return "one"
            elif x == 2:
                return "two"
            return "other"
        """,
    )

    chunks = find_if_elif_without_else(git_repo)

    assert len(chunks) == 1
    assert chunks[0].start_line == 2
    assert chunks[0].end_line == 5


def test_ignores_elif_chain_with_else(git_repo: Path) -> None:
    _commit_module(
        git_repo,
        """
        def f(x: int) -> str:
            if x == 1:
                return "one"
            elif x == 2:
                return "two"
            else:
                return "other"
        """,
    )

    assert find_if_elif_without_else(git_repo) == ()


def test_ignores_else_block_that_begins_with_an_if(git_repo: Path) -> None:
    # The else body is more than the guard, so this is a real else clause and not
    # an elif -- the two are only indistinguishable when the body is the if alone.
    _commit_module(
        git_repo,
        """
        def f(x: int, y: int) -> str:
            if x == 1:
                return "one"
            else:
                if y == 0:
                    return "guarded"
                return "other"
        """,
    )

    assert find_if_elif_without_else(git_repo) == ()


def test_finds_else_block_holding_only_an_if(git_repo: Path) -> None:
    # Written this way, the else is indistinguishable from an elif in the AST, so it
    # is read as one: the chain leaves the case where neither condition holds unhandled.
    _commit_module(
        git_repo,
        """
        def f(x: int, y: int) -> str | None:
            if x == 1:
                return "one"
            else:
                if y == 0:
                    return "zero"
            return None
        """,
    )

    assert len(find_if_elif_without_else(git_repo)) == 1


# The diagnostics ty reported on CI run 34881780066 (commit 0c13a13c, branch
# mngr/host-sizing), rendered in the concise format. ty sorts by path, which here puts its
# warnings ahead of its errors -- that is what makes this the right input for the ordering
# assertion below.
_CI_RUN_34881780066_STDOUT = """\
apps/minds_admin/imbue/minds_admin/cli/server.py:1757:16: warning[possibly-missing-submodule] Submodule `errors` might not have been imported
apps/minds_admin/imbue/minds_admin/cli/server_test.py:125:29: warning[possibly-missing-submodule] Submodule `errors` might not have been imported
apps/remote_service_connector/imbue/remote_service_connector/sync.py:428:16: warning[possibly-missing-submodule] Submodule `errors` might not have been imported
apps/remote_service_connector/imbue/remote_service_connector/sync.py:512:28: warning[possibly-missing-submodule] Submodule `errors` might not have been imported
apps/remote_service_connector/imbue/remote_service_connector/testing.py:3005:19: warning[possibly-missing-submodule] Submodule `errors` might not have been imported
apps/remote_service_connector/imbue/remote_service_connector/testing.py:3007:19: warning[possibly-missing-submodule] Submodule `errors` might not have been imported
libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/providers/instance_test.py:1700:87: error[unknown-argument] Argument `stop_kind` does not match any known parameter of function `make_workspace_info`
libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/providers/instance_test.py:1708:62: error[unknown-argument] Argument `stop_kind` does not match any known parameter of function `make_workspace_info`
libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/providers/instance_test.py:1718:62: error[unknown-argument] Argument `stop_kind` does not match any known parameter of function `make_workspace_info`
libs/mngr_kanpan/imbue/mngr_kanpan/tui.py:3785:60: warning[unused-ignore-comment] Unused `ty: ignore` directive
Found 10 diagnostics
"""


def test_counts_only_error_severity_diagnostics() -> None:
    outcome = parse_type_check_output(_CI_RUN_34881780066_STDOUT, "", 1)

    assert len(outcome.error_diagnostics) == 3
    assert len(outcome.warning_diagnostics) == 7
    assert outcome.unrecognized_output == ()
    assert outcome.is_failure


def test_failure_message_leads_with_the_diagnostics_that_caused_the_failure() -> None:
    message = parse_type_check_output(_CI_RUN_34881780066_STDOUT, "", 1).failure_message

    headline, _blank, first_diagnostic = message.splitlines()[:3]
    assert headline == "Type checker found 3 error(s) (ty exit code 1):"
    assert "error[unknown-argument]" in first_diagnostic
    assert "possibly-missing-submodule" not in message.split("warning(s)")[0]


def test_a_warning_only_run_is_not_a_failure() -> None:
    stdout = (
        "apps/minds_admin/imbue/minds_admin/cli/server.py:1757:16: "
        "warning[possibly-missing-submodule] Submodule `errors` might not have been imported\n"
        "Found 1 diagnostic\n"
    )

    outcome = parse_type_check_output(stdout, "", 0)

    assert not outcome.is_failure
    assert len(outcome.warning_diagnostics) == 1


def test_a_clean_run_reports_nothing() -> None:
    outcome = parse_type_check_output("All checks passed!\n", "", 0)

    assert not outcome.is_failure
    assert outcome.error_diagnostics == ()
    assert outcome.warning_diagnostics == ()
    assert outcome.unrecognized_output == ()


def test_a_nonzero_exit_without_diagnostics_still_fails_and_shows_the_output() -> None:
    outcome = parse_type_check_output("", "error: Failed to parse ty.toml\n", 2)

    assert outcome.is_failure
    assert outcome.failure_message.splitlines()[0] == (
        "Type checker exited 2 without reporting an error-severity diagnostic."
    )
    assert "Failed to parse ty.toml" in outcome.failure_message


def test_unparseable_stdout_is_surfaced_rather_than_dropped() -> None:
    outcome = parse_type_check_output("ty panicked at src/lib.rs:42\n", "", 101)

    assert outcome.unrecognized_output == ("ty panicked at src/lib.rs:42",)
    assert "ty panicked at src/lib.rs:42" in outcome.failure_message
