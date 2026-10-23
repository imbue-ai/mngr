# Workspace programs

The Python programs Imbue Studio runs *inside* a workspace through `mngr exec`.
Each is a real module in this package, checked by ruff, ty and the ratchets like
the rest of the app.

## Rules

- **Standard library only.** A program runs under the workspace's own `python3`,
  with none of the app's code and none of its dependencies. It may import the
  standard library and other modules of this package, nothing else.
  `test_workspace_programs_import_only_the_standard_library_and_their_own_package`
  (in `test_minds_ratchets.py`) enforces this.
- **The template image's Python.** The interpreter is the workspace image's
  `python3` (3.12), the same version the app is checked against.
- **No work at import time.** A module only defines constants and functions; a
  program does its work in `main()`, behind `if __name__ == "__main__":`. The app
  imports these modules too (for their constants), and tests can import them.
- **Parameters through argv.** Values the app knows at dispatch time (a tag, an
  agent id, a setting) travel as command-line flags.
- **One answer.** A program answers on one marker-prefixed JSON line
  (`program_support.emit_marker_json`); the app finds it with
  `extract_marker_json` however noisy the rest of the output is.

## Layout

- `program_support.py`: helpers any program can use (running a tool, reading
  argv, polling, printing the answer).
- `backup_protocol.py`, `backup_support.py`: what the backup programs share.
  The `*_protocol.py` module holds the constants the app and the programs both
  read (answer markers, the values an answer carries, the official remote), so
  each value has one source.
- `backup_check.py`, `backup_gate_probe.py`, `backup_apply_update.py`,
  `backup_restore.py`: the programs. Each module's docstring says what it does.
- `update_protocol.py`: what the update program and the app share (state paths,
  cron names, the answer marker, the outcome, skip-reason and started-by
  strings, and the text of the messages the program sends). The app reads
  those strings into its own enums in `desktop_client/update_status.py`, and
  `update_workspace_command_test.py` pins them to the enums' values.
- `update_workspace.py`: the update program (its modes `arm`, `now`, `cancel`,
  `read` and `tick`); `update_dispatcher.py`: the dispatcher its `tick` runs,
  and what the modes share with it.

## Sharing code and constants

A program imports what it shares as an ordinary module of this package
(`from imbue.minds.workspace_programs.backup_support import service_state`). A
constant the app also needs goes in a module here, and the app imports it from
there: the program and the app read one definition, and the type checker sees
both reads. Enumerated values a program reports (an outcome, a state) belong
here as plain string constants, which the program answers with and the app
compares against.

## How a program ships

`desktop_client/workspace_program_command.py` builds a zip application from a
program: the program's module as `__main__.py`, plus every module of this
package it imports (directly or through another), at their package paths. The
archive is deterministic, so the same sources always give the same bytes.
`build_workspace_program_command` sends it base64-encoded inside the
`mngr exec` command, which writes it to a temporary file, runs
`python3 <file> <args>`, and removes the file.

The archive is also a standalone file: `python3 <archive> <args>` runs it from
any file name. A program that must persist in the workspace (run later by cron,
say) writes its archive's bytes to its own path and runs from there.

## Testing

- Unit tests can import a program's module and call its functions directly.
- `desktop_client/backup_workspace_programs_test.py` runs the shipped archive
  through the real command against temporary git repositories, with stub `uv`
  and `supervisorctl` on `PATH`.
- `desktop_client/update_dispatcher_test.py` and
  `desktop_client/update_workspace_command_test.py` run the update program's
  modes against a staged workspace with stub `mngr`, `tk` and template scripts,
  in-process and through the shipped command.
- `test_workspace_programs_e2e.py` (in `apps/minds/`) runs programs inside a
  real workspace container built from the default workspace template, through
  the `running_workspace` fixture in `apps/minds/conftest.py` and
  `run_workspace_program_in_container` in `imbue/minds/testing.py`.
