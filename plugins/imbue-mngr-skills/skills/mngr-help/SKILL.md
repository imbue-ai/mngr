---
name: mngr-help
description: Use whenever the user says anything about mngr. Run `mngr help` right away to get context on what mngr does, and use `mngr ask` to turn a goal into a command.
allowed-tools: Bash(mngr help*), Bash(mngr ask *)
---

When the user says anything about mngr -- asking you to run a command, manage agents or hosts, coordinate with other agents, or just mentioning it in passing -- run `mngr help` right away so you have context on what mngr is and which commands exist.

- `mngr help` lists every command and help topic. `mngr help <command>` (equivalently `mngr <command> --help`) shows the details, options, and examples for one command.
- `mngr ask "<question>"` lets you describe what you want in plain language; mngr suggests the CLI command to run. Add `--execute` to run the suggested command directly instead of just printing it.

Reach for `mngr ask` when you know what you want to accomplish but not the exact command, and `mngr help` when you want to browse what mngr can do.
