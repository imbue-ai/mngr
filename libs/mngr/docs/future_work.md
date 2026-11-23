# Future work

The following concerns and features are out of scope for the initial versions of mngr, but may be addressed in future releases:

- **Credential scoping**: How do we prevent a child agent from using the parent's full credentials? The answer mentions "scoped-down credentials" but this is deferred.
- **Resource accounting**: If agent A spawns agent B, how is resource usage tracked and limited?
- **Secret management**: How are sensitive environment variables and files handled securely? How are API keys rotated?
- **Migration steps for `mngr upgrade`**: `mngr upgrade` rewrites the files mngr generates for an agent from the running mngr and leaves files it no longer generates in place, so it keeps no record of what an older mngr wrote. That stops being enough at the first change whose new agent state has to be derived from the agent's existing state, such as converting a state file to a new format. That change should add numbered migration steps: run by `mngr upgrade` in the agent's state directory while the agent is stopped, recorded per agent in its data.json, and safe to run twice. An agent with no record counts as having had none, so every existing agent gets the first step.

