# Machine size

Understanding this behavior corpus calls for the behaviors skill; consult it when reading this file.

This folder specifies how the desktop client lets the user choose a machine's size when a workspace is created, how it shows the size a machine has, and how it changes the size of a machine whose provider allows that.
The terms *workspace* and *machine* are defined in the [workspace glossary](../../docs/workspace/glossary.md); the decision that a size is fixed at create except where a provider can change it afterwards is recorded in mngr's hosts concept doc (`libs/mngr/docs/concepts/hosts.md`, "Sizing").

## Terms

A machine's *size* is the CPUs, memory, and disk its provider allocated to it.

A compute mode is *sized by numbers* when the create form takes its size as a CPU count, a memory amount in GB, and, where the mode has a separately sized data disk, a disk amount in GB: the local docker mode, the local lima mode, and the modal mode.
A compute mode is *sized by shape* when the create form takes its size as one named instance shape from a list: the aws, gcp, and azure connection modes.
A compute mode that offers no size at create gives every new machine the provider's own default size.

A machine's *recorded size* is the size its provider recorded for it, which the provider reports whether the machine is running or stopped.

A machine is *resizable* when its provider can change its size after it was created and the desktop client offers that change: a local docker machine (CPUs and memory), a local lima machine (CPUs, memory, and a data disk that only grows), and an Imbue Cloud machine (memory and a data disk that only grows) whose account is on a plan the client offers the change to.
An Imbue Cloud machine's memory is picked from the *sizes its plan allows*: the sizes the fleet offers (multiples of 8 GB), cut down to what the account's quota would grant once its other running machines are counted, with the machine's current size always among them.
A changed Imbue Cloud size is *pending* until the machine next restarts; the desktop client only records it, and offers the restart separately, because an Imbue Cloud restart backs the machine's disks up before it comes back.
A *plan the client offers the change to* is, today, the ally plan only; every plan's quotas bound the size the same way whether or not the client offers the change.

## Out of scope for this folder

- The default size of any compute mode, which the create form takes from the workspace template.
- The command that records an Imbue Cloud machine's size, the connector that checks its quotas and applies it, and the restart that applies it.
- What a provider does with a size it refuses; the desktop client only passes its refusal on.
