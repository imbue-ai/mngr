# mngr SSH Password Auth

SSH password authentication support for mngr. Provides `SSHPasswordAuth` for provider plugins that use password-based SSH (e.g. Daytona, Proxmox).

## Usage

Provider plugins that need password auth add `imbue-mngr-ssh-password-auth` as a dependency in their `pyproject.toml`. Importing this package auto-registers `SSHPasswordAuth` with the `SSHAuthMethod` registry via `__init_subclass__`.

## System dependencies

Requires `sshpass` for CLI transport (rsync, git). Install with:
- Debian/Ubuntu: `apt-get install sshpass`
- macOS: `brew install hudochenkov/sshpass/sshpass`
