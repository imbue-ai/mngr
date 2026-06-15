Added a `--provider` option to `mngr message` that restricts agent discovery to a single named provider instance instead of scanning every enabled provider. A caller that already knows which provider owns the target (e.g. the Minds desktop app sending a permission-response nudge) can pass it to avoid the multi-second full-provider scan.

Internally, `find_all_agents` gained a `provider_names_override` parameter that the new option threads through to discovery.
