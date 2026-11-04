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

Each test session produces a transcript with annotated lines. Stdout and stderr lines are interleaved in the order they were produced (line-buffered):

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
- `output_lines` -- interleaved stdout/stderr lines in the order they were produced

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

## Credential redaction and artifact scrubbing

A session's commands inherit the test runner's environment, so a command that
prints its environment writes every credential the runner holds into the
transcript. Two modules keep those values out of anything that gets published:

- `imbue.skitwright.redaction.redact_secret_values(text, env, secret_names)` --
  replaces every occurrence of each named variable's value in `text` with
  `<redacted NAME>`. The caller names the variables that hold credentials;
  nothing is inferred from variable names.
- `imbue.skitwright.artifact_scrub.scrub_paths(paths, env, secret_names)` --
  rewrites every UTF-8 text file under `paths` in place (directories are walked
  recursively; other files are reported as skipped): the named variables'
  values become `<redacted NAME>`, and anything else credential-shaped
  (Anthropic, LiteLLM and OpenAI keys, Modal tokens, GitHub tokens, AWS access
  key ids, private-key blocks; documentation placeholders excepted) becomes
  `<redacted PATTERN>`. The returned `ScrubReport` lists the scanned, redacted
  and skipped files and the `pattern_redactions`, each located by file and
  line; one of those means a credential the caller did not name got in.
  `secret_names_from_env(env)` reads the names to redact from the
  comma-separated `MNGR_SECRET_ENV_NAMES` and `VAULT_SECRET_ENV_NAMES`
  variables, and `format_pattern_redactions(redactions)` renders them one per
  line.
