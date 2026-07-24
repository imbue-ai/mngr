Improved help output for command groups.

The git-style help page (shown by `mngr <group> --help` and `mngr help <group>`) now includes a COMMANDS section listing a group's subcommands with their descriptions and aliases, so `mngr config --help`, `mngr snapshot --help`, and similar pages surface what you can do without hunting.

Running a command group with no subcommand (e.g. `mngr config`, `mngr snapshot`, `mngr git`, `mngr plugin`, or bare `mngr`) now consistently renders that same rich, pageable help page to stdout and exits with a usage-error status, instead of a plain, terminal-only usage line.
