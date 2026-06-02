---
name: mngr-help
description: Use whenever there's any indication that knowing about mngr would be useful. Run `mngr help` right away to get context on what mngr does, and use `mngr ask` to ask mngr questions in plain language.
allowed-tools: Bash(mngr help*), Bash(uv run mngr help*), Bash(mngr ask *), Bash(uv run mngr ask *)
---

When there's any indication that knowing about mngr would be useful -- for example the user asks you to run a command, manage agents or hosts, or coordinate with other agents -- run `mngr help` right away so you have context on what mngr is and which commands exist.

- `mngr help` lists every command and help topic. `mngr help <command>` (equivalently `mngr <command> --help`) shows the details, options, and examples for one command.
- `mngr ask "<question>"` chats with mngr in plain language: it can answer questions about mngr, and when you describe a goal it suggests the CLI command for it (printed for you to review and run yourself).

Reach for `mngr ask` to ask mngr something in plain language, and `mngr help` to browse the command and topic reference.
