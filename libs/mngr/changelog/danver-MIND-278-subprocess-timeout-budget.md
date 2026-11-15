Fourteen tests no longer carry a hand-written 30-second `pytest-timeout`: the seven `install.sh` tests, the dependency-check flow test, the issue-search test, the two `mngr create` CLI tests (dirty-tree rejection and JSONL progress streaming), and the three remote-flock acceptance tests. Every one of them runs a real child program -- bash, git, curl, `gh`, `uv run mngr`, ssh -- so each now inherits the 60 seconds imbue-common gives that class, which is more headroom than the marker gave them.

The JSONL streaming test is also marked `tmux`, and its 30-second marker had been overriding the 60 seconds that class already gets.

The `install.sh` tests gain something beyond headroom: their own `subprocess.run` deadline is 30 seconds, exactly what the pytest marker was, so which of the two fired first was a coin flip. The subprocess deadline now always wins, and an overrun is reported as a subprocess timeout instead of an opaque kill.
