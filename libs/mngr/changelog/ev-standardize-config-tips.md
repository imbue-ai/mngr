Standardized user-facing configuration remediation hints. Error and warning messages now always suggest a runnable `mngr config set` / `mngr config unset` command instead of telling you to hand-edit `settings.toml`.

A new shared helper (`imbue.mngr.remediations`) renders these hints in one canonical form, so flag order and scope no longer drift between call sites.

Provider-disable hints now recommend `--scope local` rather than `--scope user`. Because config precedence is user < project < local, a `--scope user` suggestion was silently overridden (and thus ineffective) whenever the provider was enabled at the project or local layer; writing to the local scope always takes effect.

Updated the affected messages in `mngr create --template`, the custom-agent-type "no command" error, and the provider-unavailable / not-authorized errors.
