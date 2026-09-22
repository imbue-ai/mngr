from imbue.mngr.utils.command_logging import commands_kept_out_of_logs
from imbue.mngr.utils.command_logging import is_command_logging_suppressed
from imbue.mngr.utils.command_logging import loggable_command
from imbue.mngr.utils.command_logging import withheld_command_label


def test_loggable_command_is_passthrough_with_no_scope() -> None:
    assert loggable_command("echo hello-93712") == "echo hello-93712"
    assert not is_command_logging_suppressed()


def test_loggable_command_withholds_the_command_inside_a_scope() -> None:
    secret_command = "export KEY=secret-value-40182"
    with commands_kept_out_of_logs("a script carrying a key"):
        assert is_command_logging_suppressed()
        stand_in = loggable_command(secret_command)
    assert "secret-value-40182" not in stand_in
    assert stand_in == f"<a script carrying a key, {len(secret_command)} bytes, not logged>"


def test_nested_scopes_report_the_innermost_reason_then_restore_the_outer_one() -> None:
    with commands_kept_out_of_logs("the outer reason"):
        with commands_kept_out_of_logs("the inner reason"):
            assert "the inner reason" in loggable_command("cmd")
        assert "the outer reason" in loggable_command("cmd")


def test_commands_kept_out_of_logs_resets_on_exit() -> None:
    with commands_kept_out_of_logs("a script carrying a key"):
        assert is_command_logging_suppressed()
    assert not is_command_logging_suppressed()
    assert loggable_command("echo hello-58204") == "echo hello-58204"


def test_the_stand_in_sizes_the_command_in_bytes_rather_than_characters() -> None:
    command = "echo 'héllo-77321'"
    with commands_kept_out_of_logs("a script carrying a key"):
        stand_in = loggable_command(command)
    assert stand_in == f"<a script carrying a key, {len(command.encode('utf-8'))} bytes, not logged>"
    assert f"{len(command)} bytes" not in stand_in


def test_a_locally_spawned_command_is_labelled_only_inside_a_scope() -> None:
    """None is the process label's own default, so nothing changes outside a scope."""
    command = "echo hello-18043"
    assert withheld_command_label(command) is None
    with commands_kept_out_of_logs("a script carrying a key"):
        assert withheld_command_label(command) == loggable_command(command)
