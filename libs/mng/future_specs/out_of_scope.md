# Out of Scope

There are features and use cases that are explicitly NOT goals for mng.

## No Multi-User / Team Features

We do NOT want features like sharing agents between team member, multi-user access control, 1st class support for multi-human workflows, etc. **`mng` is designed for single-user operation**.

**Rationale**: mng is philosophically aligned with the idea that your agents are YOUR agents—fully aligned to and controlled by you. All interactions with mng are considered fully private.

If users want to expose agent data to others, they can build that themselves via plugins or external tools, but it's entirely voluntary and under their control.

## No Centralized State

mng is stateless by design. There is no:

- Central database of agents
- Server process that must be running
- Synchronization between multiple mng installations

All state is stored in providers (Docker labels, Modal tags, local files) and can be reconstructed by querying them.

## No GUI

mng is a CLI tool. While agents can expose web interfaces (via ttyd, port forwarding, etc.), mng itself has no GUI. Use the terminal.
