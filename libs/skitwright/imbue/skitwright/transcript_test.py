from imbue.skitwright.data_types import CommandResult
from imbue.skitwright.data_types import OutputLine
from imbue.skitwright.data_types import OutputSource
from imbue.skitwright.transcript import Transcript


def _line(source: OutputSource, text: str) -> OutputLine:
    return OutputLine(source=source, text=text)


def _out(text: str) -> OutputLine:
    return _line(OutputSource.STDOUT, text)


def _err(text: str) -> OutputLine:
    return _line(OutputSource.STDERR, text)


def test_empty_transcript_formats_as_empty_string() -> None:
    t = Transcript()
    assert t.format() == ""


def test_single_command_transcript() -> None:
    t = Transcript()
    t.record(
        CommandResult(command="echo hello", exit_code=0, stdout="hello\n", stderr="", output_lines=(_out("hello"),))
    )
    assert t.format() == "$ echo hello\n  hello\n? 0\n"


def test_command_with_stderr() -> None:
    t = Transcript()
    t.record(
        CommandResult(
            command="bad-cmd", exit_code=1, stdout="", stderr="not found\n", output_lines=(_err("not found"),)
        )
    )
    assert t.format() == "$ bad-cmd\n! not found\n? 1\n"


def test_multiline_stdout() -> None:
    t = Transcript()
    t.record(
        CommandResult(
            command="ls", exit_code=0, stdout="a\nb\nc\n", stderr="", output_lines=(_out("a"), _out("b"), _out("c"))
        )
    )
    assert t.format() == "$ ls\n  a\n  b\n  c\n? 0\n"


def test_multiple_commands() -> None:
    t = Transcript()
    t.record(CommandResult(command="cmd1", exit_code=0, stdout="out1\n", stderr="", output_lines=(_out("out1"),)))
    t.record(CommandResult(command="cmd2", exit_code=1, stdout="", stderr="err2\n", output_lines=(_err("err2"),)))
    expected = "$ cmd1\n  out1\n? 0\n$ cmd2\n! err2\n? 1\n"
    assert t.format() == expected


def test_interleaved_stdout_and_stderr() -> None:
    t = Transcript()
    t.record(
        CommandResult(
            command="mixed",
            exit_code=0,
            stdout="output\n",
            stderr="warning\n",
            output_lines=(_err("warning"), _out("output")),
        )
    )
    assert t.format() == "$ mixed\n! warning\n  output\n? 0\n"
