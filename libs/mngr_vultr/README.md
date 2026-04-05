# mngr Vultr Provider

Vultr provider backend plugin for mngr. Runs agents in Docker containers on Vultr VPS instances.

## Configuration

Set `VULTR_API_KEY` in your environment or add `api_key` to the provider config in `~/.mngr/config.toml`.

## Usage

```bash
mngr create my-agent --provider vultr
```
