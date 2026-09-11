# mngr-file

Read, write, and list files on agents and hosts.

A plugin for [mngr](https://github.com/imbue-ai/mngr) that adds the `mngr file` command with `get`, `put`, and `list` subcommands.

## Usage

```bash
# Read a file from an agent (prints to stdout)
mngr file get my-agent config.toml

# Read a file and save locally
mngr file get my-agent config.toml --output local-config.toml

# Write a file to an agent from a local file
mngr file put my-agent config.toml --input local-config.toml

# Write stdin to a file on an agent
echo "hello" | mngr file put my-agent greeting.txt

# List files in an agent's work directory
mngr file list my-agent

# List files recursively
mngr file list my-agent -R

# List files in a specific subdirectory
mngr file list my-agent src/

# Use absolute paths (bypasses --relative-to)
mngr file get my-agent /etc/hostname
```

## Target

TARGET is either an agent or a host, decided from the text you type rather than by looking it up:

- `@NAME` (or `@NAME.PROVIDER`) always means a host.
- A host ID (`host-<hex>`), or any name containing a dot, is read as a host.
- Anything else is read as an agent.

A host whose name is a plain word -- no `host-` prefix, no dot -- is therefore only reachable in the `@` form. Write `mngr file list @my-host`, not `mngr file list my-host`; the bare form is read as an agent name and fails to resolve.

## Path resolution

Paths can be absolute or relative. Relative paths are resolved against a base directory that depends on the target type:

**Agent targets** use `--relative-to` to select the base directory:
- `work` (default): the agent's working directory
- `state`: the agent's state directory (`$MNGR_AGENT_STATE_DIR`)
- `host`: the host directory (`$MNGR_HOST_DIR`)

**Host targets** always resolve relative paths against the host directory (`$MNGR_HOST_DIR`).

## Options

### Output format

All subcommands accept the standard mngr `--format` option: `human` (the default), `json`, or `jsonl`.

`list` and `put` additionally accept a format template, like the other record-emitting mngr commands, and render one line per record:

```bash
mngr file list my-agent --format '{name} {size}'
echo hello | mngr file put my-agent greeting.txt --format '{path} {size}'
```

For `list`, a template can address any field an entry carries -- not just the displayed columns -- using the field names listed under Field selection below. For `put`, the fields are `path` and `size`. `size` renders the same way in both.

`get` does not accept a template: its outcome is the file's own bytes, and a template would replace the content asked for rather than describe it.

`get` streams the file's bytes to stdout in `human` format, and reports them base64-encoded as `content_base64` in `json`/`jsonl`. With `--output`, the bytes go to the named local file instead, and the report carries `output_path` and the size in place of the content.

`list` on a directory that does not exist is an error. An empty listing means the directory exists and holds nothing.

### Field selection (list only)

- `--fields name,size,modified` -- select which columns to display
- Available fields: `name`, `path`, `file_type`, `size`, `modified`, `permissions`

### File options (put only)

- `--mode 0644` -- set file permissions on the remote file
