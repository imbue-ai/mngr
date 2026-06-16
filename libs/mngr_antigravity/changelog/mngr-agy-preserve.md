agy (antigravity) agents now preserve their transcripts on destroy, matching the claude plugin.

- New `preserve_on_destroy` config option (default `true`): before an agy agent's state directory is deleted on destroy, its raw and common transcripts and the conversation-id history (root conversation plus the full conversation-ids list) are copied to `<local_host_dir>/preserved/<agent-name>--<agent-id>/`, mirroring the agent's state-directory layout. For remote agents the files are pulled to the local machine so they survive host destruction. Set to `false` to discard transcript data on destroy.

- Works for both online destroys and offline host destruction (where the agent state is read off the host's persisted volume).

- The agy release lifecycle test now asserts the transcripts are actually preserved on destroy (previously destroy was bare cleanup), so the feature is covered end-to-end against the real `agy` binary.

- agy's native resumable conversation store (the per-conversation SQLite files under `plugin/antigravity/home/.gemini/antigravity-cli/conversations/` that `agy --conversation` resumes from) is now also preserved on destroy, so the agent can be resumed or adopted. Only the `conversations/` subdir is preserved -- the agy oauth token, `settings.json`, and the macOS keychain symlink are excluded. Known limitation: on macOS the store is encrypted by the login-keychain "Antigravity Safe Storage" key, so a macOS-created store is readable on the same machine but not portable to a different machine or user (Linux uses a portable file-based store).
