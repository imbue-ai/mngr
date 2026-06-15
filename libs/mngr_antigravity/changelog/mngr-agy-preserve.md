agy (antigravity) agents now preserve their transcripts on destroy, matching the claude plugin.

- New `preserve_on_destroy` config option (default `true`): before an agy agent's state directory is deleted on destroy, its raw and common transcripts and the conversation-id history (root conversation plus the full conversation-ids list) are copied to `<local_host_dir>/preserved/<agent-name>--<agent-id>/`, mirroring the agent's state-directory layout. For remote agents the files are pulled to the local machine so they survive host destruction. Set to `false` to discard transcript data on destroy.

- Works for both online destroys and offline host destruction (where the agent state is read off the host's persisted volume).

- The agy release lifecycle test now asserts the transcripts are actually preserved on destroy (previously destroy was bare cleanup), so the feature is covered end-to-end against the real `agy` binary.
