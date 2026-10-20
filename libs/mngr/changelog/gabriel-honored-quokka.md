The listing collection scripts can now be given fallback host_dir locations, for providers whose hosts were created under an older layout than the one currently configured.

- `build_listing_collection_script` and `build_outer_listing_collection_script` take `fallback_host_dirs` and resolve the first candidate that holds a `data.json`, reporting the winner on a new `HOST_DIR=` line that `parse_listing_collection_output` surfaces as `host_dir`.

- The probe is the host record rather than directory existence, because a read against the wrong layout can leave an empty state directory behind.

- Providers that pass no fallbacks are unaffected.

The layouts a client will *read* are now named once, in `imbue.mngr.providers.host_dir_layouts`: `KNOWN_WORKSPACE_HOST_DIRS` lists the in-host `host_dir` locations mngr has shipped, and `host_dir_fallbacks(configured)` returns the others. That replaces the per-provider constants each reader had grown.

The layout new hosts are *written* at is still declared separately, by whoever bakes them -- `pool_bake.py` for imbue_cloud pool hosts, the workspace template's provider blocks, and `WORKSPACE_HOST_DIR` in the minds app (which cannot import mngr; its settings package has to load before the plugin does). So a future move is an edit in each of those plus one here, not a single line.

- Falling back runs in both directions. A client resolving the older `/mngr` (the built-in default, which is what a `$HOME`-scoped read gets when no project settings apply) can now read a host baked at `/home/user/.mngr`, not just the reverse.

- Only providers reading a container or VM that mngr baked opt in. The bare VPS realizer derives its host_dir per host (`/mngr/hosts/<name>`) and the ssh and local providers run on machines mngr does not own, so for them a stray `/mngr` would belong to something else.

- The docker and lima providers keep recording `host_dir` in their own per-host records at creation time instead. Those records are local and writable, so the layout is known without a probe. What they do not cover is hosts created between the layout cutover and the record field landing; that window is developer-only, because a pre-cutover host and a `$HOME`-scoped read both resolve `/mngr` and so agree anyway.

- The vps providers (vultr, ovh) have no per-host record and read at whatever their config resolves. That is sound as long as their `host_dir` setting matches the bake, which it does for the workspace template; they are not reachable from the minds app without a hand-written provider block.
