`mngr rename --host <agent> <new-name>` now renames the host of the referenced agent (previously a `[future]` placeholder). Only the provider's logical host name changes; the agent name, tmux session, env file, and git branch are untouched. Not all providers support host renaming (e.g. `ssh` host names remain user-owned).

Host names are now capped at 32 characters (`HostName`), since they end up in provider-side identifiers with their own length limits. Agent names and provider instance names are unaffected. Auto-generated host names are kept within the cap.
