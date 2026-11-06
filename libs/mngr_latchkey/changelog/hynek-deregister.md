Custom services now belong to the machine they are created on, the way credentials and policy already do, and are deregistered with their last account.

- Each machine store keeps a `config.json` of its own (the desktop's settings with that machine's custom services in place of this computer's) instead of a symlink to the desktop's; a store from an earlier build is migrated the first time it is materialized, keeping the registrations its link resolved to.

- A connect of a custom service, and a disconnect of a custom service's last account, read the machine's `config.json` first (one extra round trip) and install it again with that one registration added or removed, so a service another of the user's computers registered on the machine since this one last looked is kept. A connect of a builtin or bundled service carries no config at all; the bundled registrations reach a machine at provisioning, which no longer pushes this computer's own custom services and leaves the machine's as they are.

- `MachineCredentials.refresh` and remote provisioning adopt the custom services the machine's config registers into its machine store.

- `clear_remote_credentials` takes the config snapshot to install with the clear, and `MachineCredentials.disconnect_account` takes `is_service_deregistered`; `Latchkey.register_custom_service` takes the store directory to register in, and `Latchkey.deregister_custom_service` is its inverse. Both refresh the desktop gateway's `services.json`, which is now materialized from the union of every machine's custom services (`custom_service_registration_entries_of_every_machine`); `ServicesCatalog(is_including_every_machine=True)` reads that union, while a catalog over one latchkey directory names that machine's services alone.
