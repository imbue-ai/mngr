`mngr dependencies` was reworked so that "which dependencies count" and "whether to install" are now two orthogonal options instead of being conflated into the old `-c`/`-a`/`-i` flags. The old flags were removed and replaced by:

- `--scope core|all` (default `all`): which dependencies determine the exit code (and which `--install auto` targets). `--scope core` exits non-zero only when a *core* dependency is missing -- missing optional dependencies are tolerated and the command exits 0. `--scope all` exits non-zero if anything is missing.
- `--install none|interactive|auto` (default `none`): `none` only checks; `interactive` shows the same prompt as before; `auto` installs the in-scope missing dependencies without prompting.

Mapping from the old flags: old `-c` becomes `--scope core --install auto`, old `-a` becomes `--install auto`, and old `-i` becomes `--install interactive`.

`ssh` was reclassified from a core to an optional dependency. mngr's remote-host connectivity runs through paramiko (pure-Python, no `ssh` binary), so the `ssh` binary is only needed to attach an interactive session to a remote agent and as the transport for rsync/git over SSH -- all remote-only features, putting it in the same category as `rsync` and `unison`. The core dependencies are now `git`, `tmux`, and `jq`. `mngr connect` to a remote agent now raises a clear `BinaryNotInstalledError` if `ssh` is missing.
