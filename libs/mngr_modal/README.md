# mngr-modal

Modal provider backend plugin for [mngr](../mngr/README.md).

This plugin enables mngr to create and manage agents running in [Modal](https://modal.com) cloud sandboxes. Each sandbox runs sshd and is accessed via SSH, just like any other mngr host.

## Installation

`mngr-modal` is included by default when you install `mngr`. To install separately:

```bash
uv pip install mngr-modal
```

## Usage

```bash
# Create an agent on Modal
mngr create @.modal

# Create with custom resources
mngr create @.modal -b --cpu=2 -b --memory=4 -b --gpu=a10g

# Create with a custom Dockerfile
mngr create @.modal -b --file=path/to/Dockerfile

# Block outbound network access
mngr create @.modal -b offline
```

See `mngr create --help` for all available options.

## Configuration

Configure the Modal provider in your mngr settings:

```toml
[providers.modal]
default_cpu = 2.0
default_memory = 4.0
default_sandbox_timeout = 1800
```
