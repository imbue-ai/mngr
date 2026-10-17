Added a `share_home_npm_dir` option to the pi-coding agent type (default off).

When on, an agent's npm extension dir is a symlink to the shared `~/.pi/agent/npm`
instead of a per-agent copy. pi loads its extensions as TypeScript through jiti, whose
transpile cache is keyed by absolute path, so a per-agent copy (unique path) is a cache
miss and every newly created agent re-transpiles the whole extension set from scratch
(measured at ~16-24s for two extensions, on top of pi's ~10s baseline). Pointing all
agents at the one shared path lets that cache hit, cutting extension-heavy startup from
~30s to ~10s.

Only safe when every package pinned in `settings.json` is already installed in
`~/.pi/agent/npm` (e.g. baked into the image): the copy default exists precisely because
a shared `node_modules` that pi would write into (installing a missing package) can race
across concurrent agent startups and would mutate the user's home npm. Ignored on remote
hosts, which have no shared home npm.
