# offline_mngr_state Plugin [future]

This plugin backs up the state of all agents and hosts to a remote location so that the status of an agent can be understood even if the host is offline.

## Overview

Typically, `mngr` queries each host directly to get the current status of its agents.

However, since remote hosts stop after going idle, this can be inconvenient--you might want to know the status of an agent even if its host is currently offline.

This plugin addresses that by saving the state of all agents and hosts to a remote storage location both periodically, and whenever the host stops.

Note that this still could mean that the state could be outdated (for example, if an agent crashes).

## Storage Backends

The plugin supports multiple storage backends for saving the offline state:

- **Local Directory**: Save the state to a local directory on disk (useful for single-machine setups).
- **AWS S3**: Save the state to an S3 bucket.

## Configuration

You can configure the plugin in your `mngr` config file:

```toml
[plugins.offline_mngr_state]
# Interval (in seconds) between automatic saves
save_interval_seconds = 300
# Storage backend: "local" or "s3"
storage_backend = "local"
# Local directory path (if using local backend), defaults to ~/.mngr/plugin/offline_mngr_state/<host_id>
local_directory = "/path/to/offline_state"
# S3 bucket name (if using s3 backend)
s3_bucket = "my-mngr-offline-state-bucket"
# S3 region (if using s3 backend)
s3_region = "us-west-2"
```

## Dependencies

This plugin depends on the following:

- **AWS CLI** (if using S3 backend)
- **rsync** (if using local directory backend)

## Security

If using the S3 backend with untrusted hosts, be sure to provide a set of write-only credentials to prevent unauthorized access to the data from other agents (the plugin will check for this and error if read access is allowed).

Thus two sets of credentials are required: one with read/write access for mngr to be able to access the state, and one with write-only access for injecting into untrusted hosts.

If using the local directory backend, the plugin uses rrsync (restricted rsync) to ensure that the agent cannot read other agents' state files.
