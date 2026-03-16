# Core "Mind" Documentation


## Repository structure

This repository defines the configuration, prompts, and skills for all roles in the larger system. It is structured as follows:

- `GLOBAL.md` - this file. Shared instructions for all agent roles.
- `talking/` - the talking agent role (user-facing conversational agent).
- `thinking/` - the thinking agent role (inner monologue, event processor, orchestrator).
- `working/` - the working agent role (executes delegated tasks).
- `verifying/` - the verifying agent role (validates completed work).
- `(custom roles)/` - any other top-level folders define other custom roles

All roles may have any of the following:
- `<role>/PROMPT.md` - prompt for the agent role
- `<role>/memory/` - per-role memory directory
- `<role>/skills/` - skills available to the role.
- `<role>/<agent-harness-specific>` - configuration that is specific to the agent harness being used for this role (For example: `.claude` or `.pi` directories)

When a role is active, the agent will be started from the `<role>` directory (e.g., that will be the working directory for the agent process).
