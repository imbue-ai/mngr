Bounded per-host discovery reads so a wedged host can no longer leak background threads or be re-read on every poll.

- The per-host-bounded discovery path now threads the provider's `host_discovery_timeout_seconds` down as a hard per-command timeout on the discovery reads (the agent-directory listing and each `data.json` read). A host that connects and then stalls mid-read now self-terminates its reads (surfacing as a connection error that falls back to the host's last-known offline agents) instead of leaving an abandoned discovery thread running forever.

- Added cross-poll per-host de-duplication: a host whose previous discovery read is still in flight is not re-read on the next poll (its still-running read is reused), bounding accumulation to at most one in-flight read per host. Each such skip is logged as a warning -- with the per-command timeout in place this should essentially never fire, so it acts as a precise "host wedged past its timeout" alarm.

- Connect-phase timeouts are unchanged (already bounded); batch providers (modal / vps / imbue_cloud) are unaffected (they read all hosts in one bounded pass and spawn no per-host reads).
