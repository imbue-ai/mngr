# Out of Scope

There are features and use cases that are explicitly NOT goals for mngr.

## No Multi-User / Team Features

We do NOT want features like sharing agents between team member, multi-user access control, 1st class support for multi-human workflows, etc. **`mngr` is designed for single-user operation**.

**Rationale**: mngr is philosophically aligned with the idea that your agents are YOUR agents—fully aligned to and controlled by you. All interactions with mngr are considered fully private.

If users want to expose agent data to others, they can build that themselves via plugins or external tools, but it's entirely voluntary and under their control.

## No Centralized State

mngr is stateless by design. There is no:

- Central database of agents
- Server process that must be running
- Synchronization between multiple mngr installations

All state is stored in providers (Docker labels, Modal tags, local files) and can be reconstructed by querying them.

## No GUI

mngr is a CLI tool. While agents can expose web interfaces (via ttyd, port forwarding, etc.), mngr itself has no GUI. Use the terminal.
