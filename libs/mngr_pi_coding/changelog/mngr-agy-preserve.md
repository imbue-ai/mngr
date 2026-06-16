pi-coding agents now preserve their transcripts on destroy, matching the claude plugin.

- New `preserve_on_destroy` config option (default `true`): before a pi-coding agent's state directory is deleted on destroy, its raw and common transcripts and the recorded session-file pointer are copied to `<local_host_dir>/preserved/<agent-name>--<agent-id>/`, mirroring the agent's state-directory layout. For remote agents the files are pulled to the local machine so they survive host destruction. Set to `false` to discard transcript data on destroy.

- Works for both online destroys and offline host destruction (where the agent state is read off the host's persisted volume).

- The pi-coding release lifecycle test now asserts the transcripts are actually preserved on destroy (previously destroy was bare cleanup), extending the shared end-to-end coverage to this plugin.

- pi's native resumable session store (`plugin/pi_coding/sessions`) is now also preserved on destroy, so the conversation content itself survives -- previously only the recorded session-file pointer was kept, which dangled once the store was deleted. The credential `auth.json` is a path-separate sibling of the store and is excluded.
