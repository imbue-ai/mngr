The `minds env`, `minds pool`, `minds server`, and `minds paid` command groups moved to the new private `minds-admin` operator CLI (deleted outright here, no stubs); `minds` keeps only the app surface (`minds run`). Error messages and docs now point at `minds-admin env activate`.

The operator-only envs machinery (provisioning, per-env deploy, providers, secret lifecycle, recover, migrations, generation, local_store, health_check, mngr_agent_cleanup, r2_cleanup) moved to `apps/minds_admin`; `envs/` keeps only what the app runtime and public test surface use (docker_cleanup, primitives, paths, vault_reader). New public homes for pieces the deployment-test helpers need: `config/modal_profile.py`, `vault_reader.admin_key_from_supertokens_secret`, `config.loader.per_env_secret_services`.

The committed per-tier deploy.toml comments now reference the `minds-admin account` commands.
