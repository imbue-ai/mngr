- `mngr list` reports a Vultr host's real size (the vCPUs, RAM, and disk Vultr reports on the instance), recorded on the host record at create, instead of the 1 CPU / 1 GB placeholder. Hosts created before this release on the default `vc2-2c-4gb` plan answer from a table; any other pre-existing plan lists the size as unknown.

- The Vultr create release test asserts the size `mngr list` reports matches what Vultr says the instance has.
