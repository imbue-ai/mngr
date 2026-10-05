Six e2e tutorial tests assumed they were running on Linux and failed on every macOS shard.

Three hard-coded `/bin/true` and one `/bin/false` as a stand-in editor; macOS puts both under `/usr/bin`, so `mngr config edit` reported `Editor not found`. They now name the commands without a path, which `mngr config edit` resolves through PATH on either platform.

`test_exec_cwd` asserted `pwd` prints exactly `/tmp`, but `/tmp` is a symlink to `/private/tmp` on macOS and `pwd` prints the resolved path. It now matches whatever the host resolves `/tmp` to.

`test_tips_exec_env_inspect` checked that `env | sort` really sorted by re-sorting in Python. `sort` orders by the host's locale, which places the underscore in a name like `CLAUDE_CODE_BRIDGE_SESSION_ID` differently from Python's codepoint order, so the check disagreed with the pipeline it was verifying. It now re-sorts with `sort` itself.
