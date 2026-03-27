# mng-file

Read, write, and list files on agents and hosts.

A plugin for [mng](https://github.com/imbue-ai/mng) that adds the `mng file` command with `get`, `put`, and `list` subcommands.

## Usage

```bash
# Read a file from an agent (prints to stdout)
mng file get my-agent config.toml

# Read a file and save locally
mng file get my-agent config.toml --output local-config.toml

# Write a file to an agent from a local file
mng file put my-agent config.toml --input local-config.toml

# Write stdin to a file on an agent
echo "hello" | mng file put my-agent greeting.txt

# List files in an agent's work directory
mng file list my-agent

# List files recursively
mng file list my-agent -R

# List files in a specific subdirectory
mng file list my-agent src/

# Use absolute paths (bypasses --relative-to)
mng file get my-agent /etc/hostname
```

## Target

TARGET can be either an agent name/ID or a host name/ID. If the identifier matches both an agent and a host, an error is raised asking you to use the full ID for disambiguation.

## Path resolution

Paths can be absolute or relative. Relative paths are resolved against a base directory that depends on the target type:

**Agent targets** use `--relative-to` to select the base directory:
- `work` (default): the agent's working directory
- `state`: the agent's state directory (`$MNG_AGENT_STATE_DIR`)
- `host`: the host directory (`$MNG_HOST_DIR`)

**Host targets** always resolve relative paths against the host directory (`$MNG_HOST_DIR`).

## Options

### Output format

All subcommands support standard mng output options (`--output-format`, `--format`).

### Field selection (list only)

- `--fields name,size,modified` -- select which columns to display
- Available fields: `name`, `path`, `file_type`, `size`, `modified`, `permissions`

### File options (put only)

- `--mode 0644` -- set file permissions on the remote file
