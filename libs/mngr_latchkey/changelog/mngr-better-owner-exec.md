- The remote-gateway provisioning pass now also installs and starts the
  VM-resident owner-exec daemon (`owner_exec_vm.py`) on every remote outer
  (imbue-cloud slice VM or VPS), pinned to owner-exec v0.2.1. It runs as root,
  binds the audience `vm:<host-id>`, verifies request signatures against the VM
  root's `authorized_keys`, and signs responses with the VM's SSH host key --
  giving web-only workspaces SSH-equivalent authority to configure components
  that run outside the container. It binds ONLY the internal docker-bridge
  address the container reaches it at (never a public/wildcard interface): an
  unresolvable bridge address fails provisioning rather than binding `0.0.0.0`.
  A no-op on a local outer.
