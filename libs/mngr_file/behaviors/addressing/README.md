# Addressing

Understanding this behavior corpus calls for the behaviors skill; consult it when reading this file.

This area covers the step every subcommand performs before it does anything else: turning a target and a path into one absolute path on one machine, or refusing.

Addressing has two halves.
The target selects the machine and fixes a *base directory* on it.
The path is then resolved against that base, unless the path is absolute, in which case it stands on its own and the base is not consulted at all.

## Which base directory a target fixes

An agent target admits a choice of base, because an agent has more than one directory that a user might mean, and the user makes that choice explicitly.
The default is the agent's work directory, which is the directory a user asking about an agent's files almost always means.
The other two choices are the agent's state directory and the host directory of the host that agent runs on.

A host target admits no such choice: a host has exactly one directory mngr owns, so a host target always fixes the host directory, and asking for a base that only an agent has is a usage error rather than a silent substitution.

## What a stopped host changes

The host directory lives in a host's persisted storage and so is addressable whether or not the host is running.
A work directory does not: it lies outside the host directory, so it exists only while the host runs.

That single fact accounts for every difference this area records between a running host and a stopped one.
Reaching a stopped host requires its persisted storage to be reachable, and a request for a base outside the host directory cannot be served at all.
`stopped-hosts.feature` states those consequences; the Rules in this folder's `invariants.feature` bind the whole area.
