# mngr file behavior corpus

Understanding this behavior corpus calls for the behaviors skill; consult it when reading this file.

This corpus specifies the externally observable behavior of `mngr file` (`libs/mngr_file/`), a plugin that adds one command group to mngr.
The group reads, writes, and lists files on the machines mngr manages, from one local invocation, without the user arranging a connection of their own.

Terms carry their mngr meanings and are not redefined here: an *agent* and a *host* are mngr's identity primitives, and a *target* is mngr's agent-or-host address.
The textual form of a target, and the rule deciding whether a given text names an agent or a host, belong to mngr and not to this plugin; this corpus takes a resolved target as given and never specifies how the text was read.

Three directories on a machine matter to this corpus.
The *host directory* is where mngr keeps its own state on a host.
An agent's *state directory* is the part of the host directory belonging to that agent, and so survives that agent's host being stopped.
An agent's *work directory* is where that agent runs, and lies outside the host directory.

## The shape of every command

Each subcommand takes a target and a path and acts on exactly one file, or one directory, on exactly one machine.
Resolution is common to all of them: the target names the machine and fixes a base directory, and the path is then resolved against that base to one absolute path.
What distinguishes the subcommands is only what they do once that path is fixed.

A command is complete when it has either acted once and reported what it did, or refused and said why.
Nothing is retried at the user's expense and nothing is left half-done for the user to discover later.

## Areas

`addressing/` describes how a target and a path become one absolute path on one machine, and the conditions under which that resolution refuses.
`transfer/` describes moving one file's bytes in either direction between the local invocation and the addressed machine.
`listing/` describes reporting what a directory on the addressed machine contains.
The Rules in this folder's `invariants.feature` bind the whole corpus.

## Reaching a machine that is not running

A host that is not running still has its persisted storage, and the host directory lives there.
So the plugin serves a path under the host directory whether or not the host is running, and distinguishes the two states only where the difference is observable to the user.
This is the reason the plugin never starts a machine in order to serve a request: doing so would trade an operation the user expects to be cheap for one that is slow and externally visible.

## Out of scope

- The grammar of a target and the rule that decides agent versus host, which mngr owns and every mngr command shares.
- Discovery: how mngr learns which hosts and agents exist, and what it reports when a provider cannot be reached.
- Installation and enablement of the plugin itself.
- Every common mngr option except the selection of output format.
- The mechanism used to reach a machine, except where it changes what the user observes.
- The outcome of two invocations addressing one path at the same time.
