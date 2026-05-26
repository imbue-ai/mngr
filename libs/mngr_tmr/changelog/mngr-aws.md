## Fix snapshot launch path after provider bootstrap refactor

The AWS-provider shared-layer refactor removed the `is_for_host_creation` parameter from `get_provider_instance`, but `mngr_tmr`'s `--use-snapshot` launch path still passed it, which broke the type check. The snapshot path now calls the new `bootstrap_backend_for_host_creation(provider_name, mngr_ctx)` helper before `get_provider_instance`, matching how `mngr create` triggers one-time backend bootstrap (e.g. Modal's per-user environment).
