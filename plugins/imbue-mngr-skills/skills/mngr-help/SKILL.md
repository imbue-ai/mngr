---
name: mngr-help
description: Use when the user asks you to run mngr commands or interact with other mngr agents and you're not sure which command or flags to use. Points you to `mngr help` and `mngr ask`.
allowed-tools: Bash(mngr help*), Bash(mngr ask *)
---

When the user asks you to do something with mngr -- run a command, manage agents or hosts, or interact with other mngr agents -- and you aren't sure how, use mngr's built-in help instead of guessing at commands:

- `mngr help` lists every command and help topic. `mngr help <command>` (equivalently `mngr <command> --help`) shows the details, options, and examples for one command.
- `mngr ask "<question>"` lets you describe what you want in plain language; mngr suggests the CLI command to run. Add `--execute` to run the suggested command directly instead of just printing it.

Reach for `mngr ask` when you know what you want to accomplish but not the exact command, and `mngr help` when you want to browse what mngr can do.
