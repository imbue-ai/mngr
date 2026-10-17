# Machine size

Understanding this behavior corpus calls for the behaviors skill; consult it when reading this file.

This folder specifies how the desktop client lets the user choose a machine's size when a workspace is created, how it shows the size a machine has, and how it changes the size of a machine whose provider allows that.
The terms *workspace* and *machine* are defined in the [workspace glossary](../../docs/workspace/glossary.md); the decision that a size is fixed at create except where a provider can change it afterwards is recorded in mngr's hosts concept doc (`libs/mngr/docs/concepts/hosts.md`, "Sizing").

## Terms

A machine's *size* is the CPUs, memory, and disk its provider allocated to it.

A compute mode is *sized by numbers* when the create form takes its size as a CPU count, a memory amount in GB, and, where the mode has a separately sized data disk, a disk amount in GB: the local docker mode, the local lima mode, and the modal mode.
A compute mode is *sized by shape* when the create form takes its size as one named instance shape from a list: the bring-your-own-key cloud modes.
A compute mode that offers no size at create gives every new machine the provider's own default size.

A machine's *recorded size* is the size its provider recorded for it, which the provider reports whether the machine is running or stopped.

A machine is *resizable* when its provider can change its size after it was created and the desktop client offers that change: a local docker machine (CPUs and memory) and a local lima machine (CPUs, memory, and a data disk that only grows).
An Imbue Cloud machine's size is changed outside the desktop client, and a change there is *pending* until the machine next restarts.

## Out of scope for this folder

- The default size of any compute mode, which the create form takes from the workspace template.
- The command that changes an Imbue Cloud machine's size, and the connector that applies it.
- What a provider does with a size it refuses; the desktop client only passes its refusal on.
