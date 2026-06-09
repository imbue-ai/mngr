Fixed create-template `setting`/`setting__extend` entries being silently dropped. A `--template` whose definition sets `setting__extend = ["providers.docker.docker_runtime=runsc"]` (or any other config key) now actually reaches the resolved config instead of being ignored. Direct CLI `-S` still wins over a template-provided setting for the same key.

A template `setting` that targets `commands.*` or `create_templates.*` now raises a clear error (those sections are resolved before template settings are applied, so the value could never take effect) instead of being silently ignored.

Fixed `mngr config get` and `mngr config list --all` so they surface provider-subclass fields (e.g. `docker_runtime` on a docker provider) instead of reporting "Key not found".
