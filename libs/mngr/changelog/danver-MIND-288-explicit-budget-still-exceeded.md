Building a host no longer rescans every installed package for pyinfra's connectors. pyinfra rediscovers them on every inventory by reading a metadata file from each installed distribution, and mngr builds an inventory per host; mngr now keeps the first lookup that finds them for the life of the process.

- Commands that build several hosts no longer pay for a scan each. On a slow disk the scans had stretched host construction into tens of seconds.

- A long-running process, such as the desktop latchkey forward service, no longer fails with `KeyError: 'ssh'` after its venv is rewritten under it (#2310).

- The local and SSH providers and both outer-host builders report a missing connector as `PyinfraConnectorUnavailableError`, a mngr error, as the SSH helper already did.
