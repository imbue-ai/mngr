# Agent address syntax

Many mngr commands accept an agent address to specify which agent (and
optionally which host and provider) to target. The address format is:

```
[NAME][@[HOST][.PROVIDER]]
```

All parts are optional:

| Form | Meaning |
|------|---------|
| `NAME` | Agent name only (searches all hosts; local in create) |
| `NAME@HOST` | Agent on a specific existing host |
| `NAME@HOST.PROVIDER` | Agent on a specific host with provider disambiguation |
| `NAME@.PROVIDER` | Agent on a new host (auto-generated host name) |
| `@HOST` | Auto-named agent on an existing host |
| `@HOST.PROVIDER` | Auto-named agent on an existing host with provider |
| `@.PROVIDER` | Auto-named agent on a new auto-named host |

## Components

- **`NAME`** — The agent name. Must be a valid identifier (lowercase letters,
  digits, and hyphens). If omitted, a name is auto-generated. Without a host
  component, commands that target existing agents search across all hosts and
  providers. In `mngr create`, it defaults to the local host.
- **`HOST`** — The host name. Refers to an existing host unless `--new-host` is
  specified. If omitted with a provider (e.g. `@.modal`), a new host with an
  auto-generated name is created.
- **`PROVIDER`** — The provider backend name (e.g. `local`, `docker`, `modal`).
  Used to disambiguate when multiple providers have hosts with the same name, or
  to specify which provider should create a new host.

## Commands that accept addresses

| Command | Address use |
|---------|-------------|
| `mngr create` | Primary address argument for creating agents |
| `mngr connect` | Agent identifier (supports `@HOST.PROVIDER` disambiguation) |
| `mngr destroy` | Agent identifier(s) |
| `mngr exec` | Agent identifier(s) |
| `mngr start` | Agent identifier(s) |
| `mngr stop` | Agent identifier(s) |
| `mngr list` | `--addrs` flag outputs addresses for listed agents |

## Examples

Create an agent locally:

```bash
mngr create my-agent
```

Create an agent in a new Docker container:

```bash
mngr create my-agent@.docker
```

Create an agent on an existing Modal host:

```bash
mngr create my-agent@my-host.modal
```

Create a new named host on Modal:

```bash
mngr create my-agent@my-host.modal --new-host
```

Connect to an agent, disambiguating by provider:

```bash
mngr connect my-agent@my-host.docker
```

Destroy an agent on a specific host:

```bash
mngr destroy my-agent@my-host
```
