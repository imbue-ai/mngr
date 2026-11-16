Two per-PR tests no longer reach GitHub (MIND-745):

- `issue_reporting_test.py`'s test of the issue search's double failure sent a real request to `api.github.com` and passed only because GitHub answered quickly with an error; a stalled GitHub made it take 10.3 s against the unit lane's 10 s budget. Failing `curl` and `gh` now stand first on PATH, and the test checks that both were tried in order. It no longer patches the module's `GITHUB_REPO` either, so the `monkeypatch.setattr` ratchet drops from 31 to 30.

- `test_cli_create_from_git_url` cloned `github.com/imbue-ai/mngr` over https, and flaked when the clone dropped (MIND-253, MIND-537). It now clones a local repo through a `file://` URL, which takes the same `is_git_url` clone path. It no longer needs the network, so it moves from the acceptance lane to the integration lane, under the tmux class budget instead of a 300 s timeout.
