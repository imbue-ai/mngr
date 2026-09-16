The production tier's `deploy.toml` now commits its SSH certificate authority (`[ssh_ca] public_key`, the public half of the Vault `minds-production-ssh` CA), which gen-2 box prep, slice bakes and the connector's management-certificate refresh require; the pinned test that kept production CA-less until its bring-up is removed, since every tier now carries one.

The deploy queue (`next_deploy.md`) gains the connector migration 043 item (the overlay-address unique index and the duplicate check each tier needs before applying it).

The app-release runbook's release-channels section no longer claims beta is listed for nobody: production builds offer stable, beta and alpha, each a user's own choice in Settings.
