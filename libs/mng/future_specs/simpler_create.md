# Simplifying `mng create`

## Problem

`mng create` has ~70 CLI options across 12 option groups. This is the result of `create` being a single command that handles multiple distinct use cases:

1. Creating a local agent (the common case)
2. Creating a remote agent on a new host
3. Cloning from an existing agent
4. Running an arbitrary command as an agent

Each of these use cases pulls in its own cluster of options (host build args, source resolution, git config, env vars, provisioning, connection behavior, etc.), and many options are irrelevant or confusing when used outside their primary use case. For example, `--build-arg`, `--snapshot`, `--host-env`, `--known-host` are meaningless for local agents. `--worktree`, `--clone`, `--in-place` are meaningless for remote agents when the data must be copied anyway.

The templates system (`-t`) helps power users create shortcuts, but it doesn't reduce the surface area that a new user has to understand.

## Analysis of the current option groups

Counting the options by group (excluding the "Common" group which is shared across all commands):

| Group | Count | When relevant |
|---|---|---|
| Agent Options | 7 | Always |
| Host Options | 7 | Always (but `--host-name`, `--host-name-style`, `--tag` only for new hosts) |
| Behavior | 8 | Always, but many are advanced/scripting-only |
| Agent Source Data | 9 | Always, but `--source-agent`, `--source-host` only for cloning |
| Agent Target | 6 | Local-only (3 flags) vs. remote (irrelevant) |
| Agent Git Configuration | 6 | Only when source is a git repo |
| Agent Environment Variables | 3 | Always |
| Agent Provisioning | 7 | Mostly for remote hosts |
| New Host Environment Variables | 5 | Only when creating a new host |
| New Host Build | 5 | Only when creating a new host |
| New Host Lifecycle | 4 | Only when creating a new host |
| Connection Options | 12 | Only when `--connect` (the default) |
| Automation | 1 | Always |

Approximately 14 options are only relevant when creating a new remote host (the "New Host *" groups), and another ~12 are specific to the agent target / git / provisioning configuration that primarily matters for non-default setups.

## Ideas for splitting up the interface

### Idea 1: Extract `mng host create` as a separate command

**What:** Move all host-creation options (`--in`, build args, host env, host lifecycle, snapshots, host-name, tags) into a dedicated `mng host create` command that returns a host name/ID. Then `mng create` would only take `--host <existing-host>` to target a pre-existing host.

**Workflow:**
```bash
# Two-step (explicit):
mng host create --in modal -b gpu=h100 --idle-timeout 5m   # returns host name
mng create my-agent --host <host-name>

# One-step shorthand (unchanged):
mng create my-agent --in modal   # still works, implicitly creates host
```

**Pros:**
- Removes ~14 options from `mng create` help.
- Aligns with the conceptual model: hosts and agents are distinct concepts, so having distinct commands makes the mental model clearer.
- `mng host create` could be reused by other workflows (e.g., pre-warming hosts, creating hosts for multiple agents).
- Already partially exists conceptually -- `mng create --host <name>` already targets an existing host.

**Cons:**
- Adds a second step for the "create remote agent" flow (though the shorthand could remain).
- Introduces a new command namespace (`mng host`).
- Need to decide how `--in modal` shorthand interacts with the new command.

**Assessment: Strong idea.** This is probably the highest-leverage change. It cleanly separates two distinct concerns (host provisioning vs. agent creation) that are currently tangled together. The shorthand `--in` can remain on `mng create` as syntactic sugar that internally does both steps.

---

### Idea 2: Move connection/messaging options to `mng connect`

**What:** Remove connection-related options (--reconnect, --interactive, --retry, --retry-delay, --attach-command, --connect-command, --message, --message-file, --edit-message, --resume-message, --resume-message-file, --ready-timeout) from `mng create` and instead have `mng create` always do "create, then connect" as two discrete steps. The connection options would live on `mng connect` (many already do), and `mng create` would just pass through to `mng connect` after creation.

**Workflow:**
```bash
# Current:
mng create my-agent --message "do stuff" --reconnect --retry 5

# New: same behavior, but the connection flags live on connect
mng create my-agent --message "do stuff"
# --reconnect, --retry etc. would come from config or from `mng connect`
```

**Pros:**
- Removes ~12 options from `mng create`.
- Connection behavior is already its own concept (there's already a `mng connect` command).
- `--message` and `--edit-message` are the only connection options commonly used during create; the rest are rarely used.

**Cons:**
- `--message` is very commonly used with `mng create` and would feel awkward if moved to a separate command.
- The "create then connect" flow is tightly integrated (background forking, await-ready, etc.).

**Assessment: Moderate idea.** The rarely-used connection tuning options (--retry, --retry-delay, --reconnect, --attach-command, --connect-command) could be moved to config defaults or `mng connect` without much loss. But `--message`, `--edit-message`, and `--no-connect` are core to the create experience and should stay. A pragmatic approach: keep `--message`, `--edit-message`, `--no-connect`, and `--await-*` on create; move everything else connection-related to config or `mng connect`.

---

### Idea 3: Extract provisioning options into `mng provision`

**What:** The provisioning options (--user-command, --sudo-command, --upload-file, --append-to-file, --prepend-to-file, --create-directory, --grant) are already conceptually separate -- there's even a `mng provision` command. Move these out of `mng create` and have users either: (a) use `mng provision` after create, or (b) define provisioning in config/templates.

**Workflow:**
```bash
# Current:
mng create my-agent --in modal --user-command "pip install foo" --upload-file config.yaml:/etc/config.yaml

# New:
mng create my-agent --in modal
mng provision my-agent --user-command "pip install foo" --upload-file config.yaml:/etc/config.yaml

# Or via config/template (already possible):
mng create my-agent -t my-custom-setup
```

**Pros:**
- Removes 7 options from `mng create`.
- Provisioning is already a separate concept with its own command.
- Encourages using templates/config for repeatable setups instead of long CLI invocations.

**Cons:**
- Two-step workflow for a single logical operation.
- Provisioning during create is integrated into the agent startup flow (it happens before the agent process starts); post-create provisioning would need to stop and restart the agent.

**Assessment: Moderate idea.** The main concern is that provisioning during create happens at a specific point in the lifecycle (after file copy, before agent start) that's hard to replicate post-hoc. However, if provisioning commands are primarily used by power users with templates, removing them from the default `--help` while still supporting them (perhaps as hidden options or only via templates) could work. A middle ground: keep `--grant` on create (it's fundamental to agent setup), but move the file manipulation options (upload, append, prepend, create-directory) and shell commands to `mng provision` or templates-only.

---

### Idea 4: Collapse source/target options into a simpler model

**What:** The source/target option space is complex: `--source`, `--source-agent`, `--source-host`, `--source-path`, `--target`, `--target-path`, `--in-place`, `--copy`, `--clone`, `--worktree`, plus all the git options. Simplify by:

1. Making `--from <source>` the single source option (it already supports the compound `AGENT.HOST:PATH` format). Remove `--source-agent`, `--source-host`, `--source-path` as separate options (they become sub-parts of `--from`).
2. Having the copy mode (worktree/clone/copy/in-place) be auto-detected with smarter defaults, and only expose a single `--isolation` or `--copy-mode` enum option for overrides.

**Workflow:**
```bash
# Current (verbose):
mng create my-agent --source-agent other-agent --source-host my-host --source-path /code --worktree

# New (unified):
mng create my-agent --from other-agent.my-host:/code --copy-mode worktree
```

**Pros:**
- Reduces ~9 source/target options to 2-3.
- The separate `--source-agent`, `--source-host`, `--source-path` options are confusing when `--from` already handles the compound format.
- `--copy`, `--clone`, `--worktree`, `--in-place` as separate boolean flags are confusing (mutually exclusive flags should be a single choice).

**Cons:**
- The `--source-agent`/`--source-host`/`--source-path` options exist for scriptability (parsing compound strings in scripts is annoying).
- Deprecating options is always a migration cost.

**Assessment: Strong idea for the copy-mode consolidation.** The four mutually exclusive boolean flags (`--copy`, `--clone`, `--worktree`, `--in-place`) should definitely become a single `--copy-mode` (or similar) enum option. The source consolidation is more debatable -- the compound format is nice for humans but the separate options are useful for scripts. Could keep both but mark the separate ones as hidden/advanced.

---

### Idea 5: Better use of config defaults and profiles

**What:** Rather than splitting commands, reduce the *effective* surface area by making config defaults more powerful. The templates system already exists but could be expanded:

1. Allow a `default_template` config option so users don't even need `-t`.
2. Support provider-specific defaults (e.g., "when --in modal, default to these build args").
3. Support project-specific defaults (in `.mng/settings.toml` within the repo).

This doesn't remove options, but means most users never see or think about them.

**Pros:**
- No breaking changes.
- Solves the "too many options" problem for repeat users.
- Already partially implemented (templates, config defaults).

**Cons:**
- Doesn't help first-time users reading `--help`.
- Config-based magic can be confusing ("why did it use these settings?").

**Assessment: Complementary, not a replacement.** This should be done regardless of other changes. But it doesn't address the core problem of the `--help` output being overwhelming.

---

### Idea 6: Split git options into `mng create --git-*` sub-namespace or config

**What:** The 6 git configuration options (`--base-branch`, `--new-branch`, `--no-new-branch`, `--new-branch-prefix`, `--depth`, `--shallow-since`) are relevant only for git repos and are mostly used with their defaults. Move these to config defaults (per-project or global) and only expose `--branch` as a simplified create option.

**Workflow:**
```bash
# Current:
mng create my-agent --base-branch main --new-branch feature/foo --new-branch-prefix dev/

# New:
mng create my-agent --branch feature/foo  # single option, creates from current branch by default
# Advanced git options live in config:
# [commands.create]
# new_branch_prefix = "dev/"
# depth = 100
```

**Pros:**
- Removes 5 options from `--help` that most users never touch.
- `--branch` (or `--new-branch`) is the only git option most people actually use.

**Cons:**
- Power users who need `--depth` or `--shallow-since` would need to use config files.
- Minor: existing scripts using these options would need updating.

**Assessment: Good idea.** Most of these options have sensible defaults and are rarely overridden. The one commonly used option (`--new-branch`) should stay; the rest can be hidden or moved to config.

---

## Recommended approach (combining ideas)

Ranked by impact and feasibility:

1. **Consolidate copy-mode flags** (from Idea 4): Replace `--copy`, `--clone`, `--worktree`, `--in-place` with a single `--copy-mode` option. This is a clear improvement with no real downside. Keep the old flags as hidden aliases for backwards compatibility.

2. **Extract host creation** (from Idea 1): Create `mng host create` for explicit host provisioning. Keep `--in` on `mng create` as shorthand. This removes ~14 options from the primary help output.

3. **Trim connection options** (from Idea 2): Move `--retry`, `--retry-delay`, `--reconnect`, `--attach-command`, `--connect-command`, `--ready-timeout` to config defaults. Keep `--message`, `--edit-message`, `--no-connect`, and `--await-*` on create.

4. **Hide git tuning options** (from Idea 6): Keep `--new-branch` / `--no-new-branch`. Move `--base-branch`, `--new-branch-prefix`, `--depth`, `--shallow-since` to hidden/advanced or config-only.

5. **Improve config/templates** (from Idea 5): Add `default_template` and provider-specific defaults.

Net effect: `mng create --help` would go from ~70 options to ~30-35, with the removed options still accessible via dedicated commands, config, or hidden flags.
