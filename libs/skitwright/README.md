# skitwright

Lightweight end-to-end testing framework for CLI applications. A nod to [Playwright](https://playwright.dev/), but for command-line tools.

## Overview

skitwright provides primitives for writing end-to-end tests that exercise CLI applications through their external interface only: commands, exit codes, stdout, and stderr. No library imports from the application under test are needed.

Each test session automatically records a text transcript of all commands and their outputs, useful for debugging failures.

## Usage

```python
from imbue.skitwright.session import Session
from imbue.skitwright.expect import expect


def test_my_cli(tmp_path):
    session = Session(cwd=tmp_path)

    result = session.run("my-cli --version")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("1.0.0")

    result = session.run("my-cli do-something")
    expect(result).to_succeed()
    expect(result.stdout).to_match(r"Done in \d+ms")
```

## Transcript format

Each test session produces a transcript with annotated lines:

- `$ ` prefixes the shell command that was run
- `  ` (two spaces) prefixes each line of stdout
- `! ` prefixes each line of stderr
- `? ` prefixes the exit code

Example:

```
$ my-cli --version
  my-cli 1.0.0
? 0
$ my-cli bad-command
! Error: unknown command "bad-command"
? 1
```

## API

### Session

The `Session` class is the main entry point. It runs commands and records a transcript.

- `Session(env, cwd)` -- create a session with optional environment and working directory
- `session.run(command, timeout)` -- run a shell command, return a `CommandResult`
- `session.transcript` -- the accumulated transcript text

### CommandResult

Returned by `session.run()`. Fields:

- `command` -- the command string
- `exit_code` -- integer exit code
- `stdout` -- captured standard output
- `stderr` -- captured standard error
- `succeeded` -- `True` if exit code is 0
- `failed` -- `True` if exit code is not 0

### expect()

Fluent assertion API:

- `expect(result).to_succeed()` -- assert exit code 0
- `expect(result).to_fail()` -- assert exit code != 0
- `expect(result).to_have_exit_code(n)` -- assert specific exit code
- `expect(string).to_contain(substring)` -- assert substring present
- `expect(string).to_match(pattern)` -- assert regex match
- `expect(string).to_equal(expected)` -- assert exact equality
- `expect(string).to_be_empty()` -- assert empty string
- `expect(string).not_to_contain(substring)` -- assert substring absent
- `expect(string).not_to_match(pattern)` -- assert no regex match
