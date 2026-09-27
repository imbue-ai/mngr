# mngr Vultr Provider

Vultr provider backend plugin for mngr. Runs agents in Docker containers on Vultr VPS instances.

See `mngr_vps` for the base architecture and shared infrastructure.

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
mngr create my-agent --provider vultr -b --vultr-region=sjc -b --vultr-plan=vc2-2c-4gb
mngr list
mngr exec my-agent "echo hello"
mngr stop my-agent
mngr start my-agent
mngr destroy my-agent
```

## Vultr-specific configuration

These fields extend the base `VpsProviderConfig` (see `mngr_vps`):

<!-- BEGIN GENERATED CONFIG TABLE (scripts/make_cli_docs.py) -->
| Field | Default | Description |
|---|---|---|
| `backend` | `vultr` | Provider backend (always 'vultr' for this type) |
| `api_key` | `None` | Vultr API key. Falls back to VULTR_API_KEY env var. |
| `default_region` | `ewr` | Default Vultr region |
| `default_plan` | `vc2-2c-4gb` | Default Vultr plan |
| `default_os_id` | `2136` | Default Vultr OS ID (Debian 12 x64) |
<!-- END GENERATED CONFIG TABLE -->
