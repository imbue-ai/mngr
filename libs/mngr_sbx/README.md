# mngr Docker Sandboxes Provider

[Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) (`sbx`) provider backend plugin for mngr. Runs agents inside Docker-managed sandboxes with SSH access bridged via `sbx exec` and `sbx ports`.

## Prerequisites

- [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) (`sbx` CLI on PATH)
- A Docker account; complete `sbx login` once before using this provider

## Usage

```bash
# Install the plugin
uv tool install imbue-mngr-sbx

# Create a host in a Docker sandbox
mngr create @.sbx

# Create with an additional workspace mounted into the sandbox
mngr create @.sbx -b "workspace=/path/to/docs:ro"
```

## Authentication

`sbx` authenticates to Docker via an OAuth 2.0 Device Authorization Grant. The login is a one-time browser step:

```bash
sbx login
```

This writes a refresh-token-backed credential to the local sbx state directory. Subsequent `mngr create @.sbx` calls use that credential transparently. If the credential is missing or expired, the provider reports a clear error pointing back at `sbx login`.

For headless or CI use, pre-authenticate `sbx` on a host you control, then mount the resulting sbx state directory into the headless environment. There is no programmatic token-set form today.
