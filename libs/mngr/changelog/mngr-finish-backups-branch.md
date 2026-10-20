`mngr exec` can now stream a command's output live instead of only printing it after the command finishes.

- Added `mngr exec --stream`, which writes each stdout/stderr line as it arrives (human output format, intended for a single agent). Without it, `mngr exec` still buffers and prints everything at the end, as before.

- `HostInterface.execute_stateful_command` gained an optional `on_output(line, is_stdout)` callback: when provided, the command's output is streamed to it line-by-line (via the same paramiko read loop as `execute_streaming_command`) and the full result is still returned. `exec_command_on_agents` threads an `on_output` through to it. This is what makes long minds workspace operations (e.g. an in-place backup restore) show their progress as it happens rather than all at once at the end.
