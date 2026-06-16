Codex agents now preserve their transcripts on destroy (closing the carried-forward session-preservation gap), matching the claude plugin.

- New `preserve_on_destroy` config option (default `true`): before a codex agent's state directory is deleted on destroy, its raw and common transcripts and the root session-id history are copied to `<local_host_dir>/preserved/<agent-name>--<agent-id>/`, mirroring the agent's state-directory layout. For remote agents the files are pulled to the local machine so they survive host destruction. Set to `false` to discard transcript data on destroy.

- The native resumable rollout session store under `CODEX_HOME/sessions` is now preserved on destroy too, so a preserved agent can be resumed/adopted from codex's own session files. Only the `sessions/` directory is targeted, so the auth-token symlink and config that sit as siblings in `CODEX_HOME` are still excluded.

- Works for both online destroys and offline host destruction (where the agent state is read off the host's persisted volume).

- The codex release lifecycle test now asserts the transcripts are actually preserved on destroy (previously destroy was bare cleanup), so the feature is covered end-to-end against the real `codex` binary.
