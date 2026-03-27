# mng-modal

Modal provider backend plugin for [mng](../mng/README.md).

This plugin enables mng to create and manage agents running in [Modal](https://modal.com) cloud sandboxes. Each sandbox runs sshd and is accessed via SSH, just like any other mng host.

## Installation

`mng-modal` is included by default when you install `mng`. To install separately:

```bash
uv pip install mng-modal
```

## Usage

```bash
# Create an agent on Modal
mng create @.modal

# Create with custom resources
mng create @.modal -b --cpu=2 -b --memory=4 -b --gpu=a10g

# Create with a custom Dockerfile
mng create @.modal -b --file=path/to/Dockerfile

# Block outbound network access
mng create @.modal -b offline
```

See `mng create --help` for all available options.

## Configuration

Configure the Modal provider in your mng settings:

```toml
[providers.modal]
default_cpu = 2.0
default_memory = 4.0
default_sandbox_timeout = 1800
```
