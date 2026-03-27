# ProviderInstance Spec

A **ProviderInstance** is a configured endpoint that creates and manages [hosts](./host.md). Each provider instance is created by a [ProviderBackend](../imbue/mngr/interfaces/provider_backend.py), which defines the type of infrastructure (Docker, Modal, local, etc.).

For the conceptual overview, see [providers](../docs/concepts/providers.md) and [provider backends](../docs/concepts/provider_backends.md).

## Design Philosophy

The ProviderInstance follows a stateless, delegation-based design:

- **Stateless**: ProviderInstance stores no in-memory state. All host state is stored via provider-native mechanisms (Docker labels, Modal tags, local JSON files). This allows multiple mngr instances to manage the same hosts.
- **Single responsibility**: ProviderInstance handles only host lifecycle operations. Host-level operations (command execution, file I/O) are handled by the [Host class](./host_class.md) using pyinfra connectors that ProviderInstance creates.
- **Uniform interface**: All provider implementations expose the same interface. Provider-specific behavior is encapsulated within each implementation.

## Relationship with ProviderBackend

```
ProviderBackend (factory)          ProviderInstance (configured endpoint)
─────────────────────────          ───────────────────────────────────────
docker backend            ──────►  docker instance
                          ──────►  remote_docker instance (ssh://server)

modal backend             ──────►  my-modal-prod app
                          ──────►  my-modal-dev app

local backend             ──────►  your local machine
```

A ProviderBackend is a parameterized factory. A ProviderInstance is a concrete, configured endpoint created by that factory. Users define provider instances in their mngr settings:

```toml
[[providers]]
name = "my-modal-prod"
backend = "modal"
environment = "production"
```

## State Storage

ProviderInstance stores no in-memory state. All host state is stored via provider-native mechanisms (Docker labels, Modal tags, local JSON files). This allows multiple mngr instances to manage the same hosts.

### Connection Information

When creating a host, all information necessary to connect to that host must be stored via tags or other provider-level mechanisms. This includes:
- SSH username (if not the default)
- Any custom SSH ports
- Network/VPC identifiers (if applicable)
- Region or availability zone information
- Any other connection parameters specific to the provider

This ensures that mngr can always reconstruct how to connect to a host by querying the provider, without needing to maintain a separate database of connection information.

### Build State Tracking [future]

In addition to tracking created hosts, providers need to track build state for hosts that failed during the build phase. This state should be stored on the local filesystem at `~/.mngr/providers/<provider_name>/`.

This allows mngr to:
- Show hosts in the "failed" state even though no actual host was created
- Provide error information about why the build failed
- Clean up failed build artifacts

Like destroyed hosts, failed build records should persist for a configurable amount of time before being automatically cleaned up.

## Host Collection

When collecting hosts from provider instances, the following behaviors are important:

### Fail Quickly for Offline Providers [future]

When a provider is unreachable (e.g., no network connection, provider API is down), mngr should fail quickly rather than hanging or timing out slowly. This is especially important when working offline.

Providers should implement short timeouts for connectivity checks and return early if the provider is clearly unavailable.

### Host Listing Cache [future]

mngr should always cache the results of listing hosts from each provider. The cache should include:
- The list of host IDs and their states
- The exact provider configuration used for the query
- A timestamp of when the list was retrieved

This cache enables several important behaviors:

1. **Destroyed Host Detection** [future]: If a host appears in the cache but not in a fresh query (and the provider is reachable and the config is unchanged), the host should be shown as "destroyed"

2. **Offline Operation** [future]: When a provider is offline, cached results allow readonly operations like `mngr list` to continue working

3. **Performance**: Avoids repeatedly querying slow provider APIs

The cache should persist for a configurable amount of time [future] (e.g., 24-48 hours by default). After this time, missing hosts are removed from the cache to avoid showing stale destroyed hosts indefinitely.
