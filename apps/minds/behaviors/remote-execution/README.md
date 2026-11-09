# Remote execution

Understanding this behavior corpus calls for the behaviors skill; consult it when reading this file.

Remote execution is the desktop client letting the agents of an approved workspace run commands on this computer, as the user, through the agent-facing API.
The *switch* is this device's own setting (Settings > This computer, "Allow agents to run commands on this computer"): off on a fresh install, stored on this device alone, and never carried to the user's other devices.
A *remote-execution grant* is the per-workspace permission that admits one workspace's agents to one device's route; the user gives it by approving a request in the request popup, which carries the warning.
A grant admits either any command or one *program*: the executable named by the route's `program` query parameter, a bare name the device resolves on the user's PATH or a `~`-rooted or absolute path the agent already knows (for example a script it wrote through file sharing).

## Out of scope

- What the command's output looks like on the wire, beyond that it is streamed and never capped.
- The template-side skill that tells agents when to use this and when not to.
