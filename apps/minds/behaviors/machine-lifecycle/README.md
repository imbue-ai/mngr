# Machine lifecycle

Understanding this behavior corpus calls for the behaviors skill; consult it when reading this file.

This folder specifies what the desktop client does about a remote (imbue cloud) machine that is not running because someone asked for that: the owner from another device, an operator, or the account's suspension.
The lifecycle vocabulary (running, stopping, stopped, starting) and *stop kind* are defined in the [workspace glossary](../../docs/workspace/glossary.md); the design is [`specs/workspace-stop-kinds.md`](../../../../specs/workspace-stop-kinds.md).

A machine is *held* when its stop kind is `maintenance` or `suspension`, or one this build does not recognize: its stop is not the owner's to end.
A machine is *owner-startable* when its stop kind is `owner`, `idle`, or not recorded.
A machine is *stopped on purpose* when the connector reports it stopping, stopped or starting, whatever its kind.

## Out of scope for this folder

- Machines on the user's own device (docker, lima) and bring-your-own-key cloud machines, whose hosts can stop on their own; the app keeps starting those when they stop answering.
- How the operator tooling stops a machine or picks its kind (`minds-admin`, the migration runbook).
