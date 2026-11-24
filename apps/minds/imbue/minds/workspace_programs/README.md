# Workspace programs

The Python programs Imbue Studio runs *inside* a workspace through `mngr exec`.
Each is a real module in this package, checked by ruff, ty and the ratchets like
the rest of the app, except the two ratchets against bare `print` and direct
`subprocess` calls: a program answers on stdout and runs the workspace's tools
itself.

## Rules

- **Standard library only.** A program runs under the workspace's own `python3`,
  with none of the app's code and none of its dependencies. It may import the
  standard library and other modules of this package, nothing else.
  `test_workspace_programs_import_only_the_standard_library_and_their_own_package`
  (in `test_minds_ratchets.py`) enforces this.
- **The template image's Python.** The interpreter is the workspace image's
  `python3` (3.12), the version the app's tests run on. ruff and ty check the
  package against 3.11, the repo's lowest supported version (`requires-python`
  in the root `pyproject.toml`).
- **No work at import time.** A module only defines constants, types and
  functions; a program does its work in `main`, behind
  `if __name__ == "__main__":`. The app imports these modules too (for their
  constants), and tests can import them.
- **Parameters through argv.** Values the app knows at dispatch time (a tag, an
  agent id, a setting) travel as command-line flags, and no helper reads the
  process's argv.
  - A backup program's entry point is `main(argv)`, called as
    `main(sys.argv[1:])`. It reads every flag (`program_support.argv_value`,
    `has_argv_flag`) and passes the values down as arguments, so a test can
    call `main` with the arguments it wants.
  - The update program's `main` takes its mode from the first argument and
    hands that mode a `ProgramContext` (`update_dispatcher.py`): the remaining
    arguments, the program's own archive path, and the time, directories and
    environment the run acts on. Each mode reads its own flags from the
    context, and a test builds one with `update_program_context`
    (`desktop_client/testing.py`) and calls the mode directly.
- **One answer.** A program answers on one marker-prefixed JSON line
  (`program_support.emit_marker_json`); the app finds it with
  `extract_marker_json` however noisy the rest of the output is. The update
  program's `tick` mode, which cron runs, answers nothing.

## Layout

- `program_support.py`: helpers any program can use (running a tool, reading
  argv, polling, printing the answer), and `BACKGROUND_TASKS_CLI_PATH`, where
  the template keeps its background-task CLI.
- `backup_protocol.py`, `backup_support.py`: what the backup programs share.
  `backup_protocol.py` holds the constants the app and the backup programs
  both read (answer markers, the values an answer carries, the official
  remote).
- `backup_check.py`, `backup_gate_probe.py`, `backup_apply_update.py`,
  `backup_restore.py`, `backup_verification_flag.py`: the programs. Each
  module's docstring says what it does.
- `update_protocol.py`: the update program's constants: its state and cron
  paths, the skip, dismiss and sign-in-check flags the app passes it, its
  answer marker, the `busy` mode's name and answer field, the outcome,
  skip-reason and started-by strings it reports, and the text of the messages
  it sends. The app imports what it reads from here, except the outcome,
  skip-reason and started-by strings: it keeps its own enums for those in
  `desktop_client/update_status.py`, and `update_workspace_command_test.py`
  pins the enums' values to the strings.
- `update_workspace.py`: the update program (its modes `arm`, `now`, `cancel`,
  `read`, `tick` and `busy`); `update_dispatcher.py`: the dispatcher its `tick` runs,
  and what the modes share with it.
- `background_task_markers.py`: reads the template's background-task marker
  files where the template has no CLI of its own to read them, for the
  dispatcher's watchdog and the `busy` mode.

## Sharing code and constants

A program imports what it shares as an ordinary module of this package
(`from imbue.minds.workspace_programs.backup_support import service_state`). A
constant the app also needs goes in a module here, and the app imports it from
there: the program and the app read one definition, and the type checker sees
both reads. Most such constants live in a `*_protocol.py` module; a few live in
the module they belong to (`backup_restore.RESUME_SERVICES_COMMAND`,
`update_dispatcher.DISPATCHER_ARCHIVE_PATH`). Flag names are the exception:
apart from the few in `update_protocol.py`, the app and the programs each spell
out the flags they pass and read.

The values a program reports come in three kinds:

- **A backup program's status, code state and service state**
  (`BackupProgramStatus`, `BackupCodeState`, `BackupServiceState`) are stdlib
  `StrEnum`s in `backup_protocol.py`, each with a parse function beside it that
  maps a missing or unrecognized value to the safe member
  (`parse_backup_program_status` reads anything else as `FAILED`). The program
  answers with a member's value; the app parses the answer and matches on the
  enum with `assert_never`, so adding a member is a type error wherever it is
  not handled.
- **A backup program's failure reason** (`BackupProgramFailureReason`, also a
  `StrEnum` there) has no parse function. The app's answer models in
  `desktop_client/backup_update.py` validate it as the enum itself: the
  programs ship from this package, so an unknown reason is a bug, and
  validation surfaces it.
- **The update program's outcome, skip reason and started-by stamp** are plain
  strings in `update_protocol.py`, which the app reads into its own enums (see
  Layout). The app reads a value it does not know as absent rather than
  failing.

## How a program ships

`desktop_client/workspace_program_command.py` builds a zip application from a
program: the program's module as `__main__.py`, plus every module of this
package it imports (directly or through another), at their package paths. The
archive is deterministic, so the same sources always give the same bytes.
`build_workspace_program_command` sends it base64-encoded inside the
`mngr exec` command, which writes it to a temporary file, runs
`python3 <file> <args>`, and removes the file. A caller can put environment
assignments in front of `python3`: the update program runs with the image's
tools first on `PATH` and mngr's unknown config tolerated
(`in_workspace_mngr.build_in_workspace_command`).

The archive is also a standalone file: `python3 <archive> <args>` runs it from
any file name. A program that must persist in the workspace (run later by cron,
say) writes its archive's bytes to its own path and runs from there.

The app's update-run probe is the one reader that runs an installed copy rather
than shipping its own: in a template without the background-task CLI it runs
the installed dispatcher's `busy` mode, so it sends no archive on every poll.
Where no copy is installed, or the installed one predates that mode, the probe
reads no chat as busy.

## Testing

- Unit tests can import a program's module and call its functions directly
  (`workspace_programs/update_dispatcher_test.py`), or call a backup program's
  `main` in-process (`desktop_client/backup_verification_flag_test.py`).
- `desktop_client/workspace_program_command_test.py` checks the archive (the
  modules it holds, bytes that depend only on the sources), the command that
  ships it, and `extract_marker_json`.
- `desktop_client/backup_workspace_programs_test.py` runs the shipped archive
  through the real command against temporary git repositories, with stub `uv`
  and `supervisorctl` on `PATH`.
- `desktop_client/update_dispatcher_test.py` and
  `desktop_client/update_workspace_command_test.py` run the update program's
  modes against a staged workspace with stub `mngr`, `tk` and template scripts,
  in-process and through the shipped command.
- `desktop_client/update_apply_window_test.py` runs the app's update-run probe
  script, including against an installed archive's `busy` mode.
- `test_workspace_programs_e2e.py` and `test_snapshot_resume.py` (in
  `apps/minds/`) run programs inside a real workspace container built from the
  default workspace template, through the `running_workspace` fixture in
  `apps/minds/conftest.py`. They ship backup programs with
  `run_workspace_program_in_container` and run the update program's commands
  with `exec_in_workspace_container` (both in `imbue/minds/testing.py`). See
  [the testing overview](../../../docs/testing-overview.md) for how these
  suites run.
- `imbue/minds/test_backup_excludes_alignment.py`, a release test, checks that
  `backup_restore`'s default snapshot excludes match the template backup
  service's.
