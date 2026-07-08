# SSH Provider

The SSH provider runs agents on **pre-existing hosts** that you reach over SSH (for example, a VM or bare-metal box you already own). Unlike cloud providers, it does **not** create, destroy, stop, start, or snapshot hosts -- the machines must already exist and be reachable. mngr connects to them via pyinfra's SSH connector and manages agents on top.

Hosts are **statically configured** in your mngr settings (or registered dynamically; see [Dynamic hosts](#dynamic-hosts) below). The SSH provider does not support creating hosts, so you target an existing configured host by name rather than asking for a new one.

## Configuration

Define an SSH provider instance and one table per host under it:

```toml
[providers.my-ssh-pool]
backend = "ssh"
# host_dir = "/tmp/mngr"   # optional: where mngr keeps its state on the remote hosts

[providers.my-ssh-pool.hosts.myvm]
address = "192.168.1.100"          # hostname or IP (required)
port = 22                          # optional, default 22
user = "root"                      # optional, default "root"
key_file = "~/.ssh/id_ed25519"     # optional: path to the SSH private key (~ is expanded)
# known_hosts_file = "~/.ssh/known_hosts"  # optional: enables strict host-key checking
```

You can list as many hosts as you like under one pool:

```toml
[providers.my-ssh-pool.hosts.web1]
address = "10.0.0.11"

[providers.my-ssh-pool.hosts.web2]
address = "10.0.0.12"
user = "ubuntu"
key_file = "~/.ssh/deploy_key"
```

### Host fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `address` | yes | -- | SSH hostname or IP address |
| `port` | no | `22` | SSH port |
| `user` | no | `root` | SSH username |
| `key_file` | no | none | Path to the SSH private key (`~` is expanded). If omitted, your ssh-agent / default keys are used. |
| `known_hosts_file` | no | none | Path to a `known_hosts` file. When set, strict host-key checking is enabled for that host. |

## Usage

Because the SSH provider cannot create hosts, target an **existing** configured host using the `NAME@HOST.PROVIDER` address form (`HOST` is the host name from your config, `PROVIDER` is the pool name):

```bash
# Run a claude agent named "my-agent" on the configured host "myvm" in "my-ssh-pool"
mngr create my-agent@myvm.my-ssh-pool claude
```

List what the provider can see:

```bash
mngr list --provider my-ssh-pool
```

Do **not** use the `NAME@.PROVIDER` form (or `--new-host`) with the SSH provider -- that asks mngr to create a new host, which this provider does not support and will reject.

## Dynamic hosts

In addition to the static `hosts` tables above, an SSH pool reads hosts from a TOML file so other tooling can register hosts at runtime. By default this is:

```
<profile_dir>/providers/<instance-name>/dynamic_hosts.toml
```

Override the path with `dynamic_hosts_file` on the provider:

```toml
[providers.my-ssh-pool]
backend = "ssh"
dynamic_hosts_file = "~/.mngr/my-ssh-pool-hosts.toml"
```

The file uses the same per-host schema as the static `hosts` tables (one `[<host-name>]` table per host). If a host name appears in both the static config and the dynamic file, the static config wins. Malformed entries in the dynamic file are skipped with a warning rather than aborting discovery.

## Limitations

The SSH provider connects to machines it does not own, so several provider features are intentionally unsupported and will raise if invoked:

- **No host lifecycle**: cannot create, destroy, stop, or start hosts (they are pre-existing).
- **No snapshots**: there is no cloud infrastructure to snapshot.
- **No volumes**.
- **No tags**: hosts are statically configured, so tags are neither stored nor mutable.
- **No outer host**: `mngr exec --outer` has nothing to target (the configured machine *is* the host).
