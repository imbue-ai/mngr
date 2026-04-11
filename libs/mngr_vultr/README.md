# mngr Vultr Provider

Vultr provider backend plugin for mngr. Runs agents in Docker containers on Vultr VPS instances.

See `mngr_vps_docker` for the base architecture and shared infrastructure.

## Setup

Set `VULTR_API_KEY` in your environment or add `api_key` to the provider config in `~/.mngr/config.toml`:

```toml
[providers.vultr]
backend = "vultr"
api_key = "YOUR_VULTR_API_KEY"
```

## Usage

```bash
mngr create my-agent --provider vultr
mngr create my-agent --provider vultr -b --region=sjc -b --plan=vc2-2c-4gb
mngr list
mngr exec my-agent "echo hello"
mngr stop my-agent
mngr start my-agent
mngr destroy my-agent
```

## Vultr-specific configuration

These fields extend the base `VpsDockerProviderConfig` (see `mngr_vps_docker`):

| Field | Default | Description |
|-------|---------|-------------|
| `api_key` | `None` (falls back to `VULTR_API_KEY` env var) | Vultr API key |
| `default_region` | `ewr` | Default Vultr region |
| `default_plan` | `vc2-1c-1gb` | Default Vultr plan |
| `default_os_id` | 2136 | Default Vultr OS ID (Debian 12 x64) |

## Implementation details

- Uses raw HTTP calls to the Vultr API v2 (`https://api.vultr.com/v2`), no third-party SDK
- VPS instances are tagged with `mngr-provider=<name>` and `mngr-host-id=<id>` for discovery
- SSH keys are uploaded to the Vultr SSH key store and referenced by ID during instance creation
- Discovery works by listing all Vultr instances with matching tags, then SSH-ing to each VPS to read host records from the state volume
