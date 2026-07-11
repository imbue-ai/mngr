The `mngr extras config` walk-through (and `mngr extras -i`) now seeds the recommended docker host-volume isolation setting.

When the default docker provider does not already configure `isolate_host_volumes`, the interactive walk-through writes `providers.docker.isolate_host_volumes = true` to your user-scope config -- creating the config file if you have none yet. This opts new installs into the forthcoming default (each host container sees only its own `host_dir` sub-folder) and silences the one-shot deprecation warning that fresh installs otherwise hit the first time they use docker. It prints how to switch back to the legacy shared-volume behavior (`mngr config set providers.docker.isolate_host_volumes false --scope user`) if you prefer it.

With `-y` or without an interactive terminal, it prints the suggested `mngr config set` command instead of writing. `mngr extras` status now reports whether docker isolation is configured.
