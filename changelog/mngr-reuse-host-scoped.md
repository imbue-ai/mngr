## `mngr create --reuse` now matches the address's host, not just the agent name

`mngr create <agent>@<host>.<provider> --reuse` previously matched any existing
agent with the same name on the same provider, ignoring the host part of the
address whenever the host was being newly provisioned. A fresh-host create with
`--reuse --update` could accidentally adopt an unrelated same-named agent on a
different host, push the new work tree onto it, and fail with
`refusing to update checked out branch`. The match is now scoped to the host
component of the address (`HostId` matches exactly; `HostName` matches by name,
and when the address also pins a provider the host's provider must match too,
so same-named hosts on different providers cannot cross-match), so the reuse
path stays anchored to the host the user actually asked for. The bare-name
form (`--reuse` without a host in the address) keeps its documented "any host"
behavior.

`mngr create` also rejects `--reuse --new-host` with a `UserInputError`:
`--new-host` always provisions a fresh host, while `--reuse` looks up an
existing agent on an existing host, so the combination has no coherent
meaning. The imbue_cloud provider is exempted so the lease/adopt flow that
the minds caller depends on continues to work.
