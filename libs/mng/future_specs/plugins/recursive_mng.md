# Recursive mng Invocation [future]

This plugin enables agents to remote / untrusted child agents by invoking `mng`.

By default, agents can create "local" sub-agents, but this plugin enables remote / untrusted agents and lineage tracking (e.g., understanding the parent-child relationships).

## New limits

- `max_depth`: Maximum allowed depth of child agents (default: unlimited). Can be used to prevent infinite recursion.
- `max_children`: Maximum number of child agents this agent can create (default: unlimited). Can be used to limit resource consumption.

## New Properties

- `parent_ids`: List of parent agent IDs, from immediate parent to root ancestor.
- `is_orphanable`: Boolean indicating if child agents should become orphans when the parent is destroyed (default: `False`). See [Orphans](#orphans) section for details.
- `created_by`: ID of the agent that created this agent (immediate parent), or `null` if created by a user.

## Orphans

When a parent agent is destroyed, what happens to its children is defined by the value of `is_orphanable` property of the agent:

- If `is_orphanable` is `False` (the default), child agents should be considered destroyed when the parent is destroyed or unreachable.
- If `is_orphanable` is `True` child agents become orphans and continue running.

By default, it is best to set `is_orphanable` to `False`, since it avoids resource leaks from orphaned child agents.

## Online-only

This plugin effectively relies on the orignial `mng` instance to run a small server and remain online, since otherwise child agents cannot be created (no ability to sign the details for their new hosts).

## Config

We have to be careful about what config settings get transferred over to child agents.

For example, there can be unversioned configs in the project folders. We need to be explicit about what gets transferred over. It may be safest to export a single consolidated config for the child agent and specify that it use that.

Open question: but how does that play with work_dir-level configs if there are multiple different work_dirs / projects involved? Perhaps it would be better to simply make sure that everything is transferred "as is", except to do a bit of re-writing for the user-level config (so that the paths work out, etc)

## Scoped-Down Credentials

Newly created child agents inherit *at most* the credentials and capabilities of their parent agents.

Scopes can be removed when creating child agents, but new capabilities cannot be added unless the creation is being done by a user directly.

The limits can be scoped down, but not increased.

## Security Concerns

There are 2 classes of security concerns with recursive agent creation:

- Excessive resource consumption (e.g., fork bombs)
- Credential leakage (e.g., child agents accessing parent's credentials when they shouldn't)

The limits help with the first (though are not foolproof).

## Future Work

This plugin effectively serves as a "control tower"--a centralized server for serving auth, creating hosts, doing things with API keys, etc.
