Hardened the `mngr message` tutorial e2e tests so they are not derailed by provider backends that are not part of the published tool.

- The e2e fixture now whitelists the backends the suite actually exercises (`local`, `docker`, `modal`, `ssh`) via `enabled_backends`. The credential-requiring cloud backends (`aws`, `gcp`, `vultr`) are only present in the dev/CI checkout (installed by `uv sync --all-packages`), not in the published wheel; left enabled they were queried on every `mngr list` fan-out and, lacking credentials, made `mngr list` exit non-zero even when the reachable providers listed fine.

- `test_message_filtered_via_stdin_delivers_to_matching_agents` now scopes its standalone count-verification `mngr list` to `--provider local` so it no longer depends on remote backends (e.g. docker) being reachable, and dropped its superfluous `@pytest.mark.rsync` mark (messaging local in-place agents syncs no files).
