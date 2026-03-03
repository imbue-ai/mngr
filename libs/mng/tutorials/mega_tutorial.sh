#!/bin/bash
set -euo pipefail
# This is a (very long) tutorial that covers most of the features of mng with examples, as well as simple ways to do common tasks.
# See the README.md for details on installation and higher level architecture.

##############################################################################
# CREATING AGENTS
#   One of the most common things you'll want to do with mng is create agents. There are *tons* of options,
#   so basically any workflow you want should be supported.
##############################################################################

## BASIC CREATION

# running mng is strictly better than running claude! It's less letters to type :-D
# running this command launches claude (Claude Code) immediately *in a new worktree*
mng
# that happens because the defaults are the following: command=create, agent=claude, provider=local, project=current dir

# if you want the default behavior of claude (starting in-place), you can specify that:
mng --in-place
# mng defaults to creating a new worktree for each agent because the whole point of mng is to let you run multiple agents in parallel.
# without creating a new worktree for each, they will make conflicting changes with one another.

# running this:
mng

# is really just the same thing as this:
mng create

# when creating agents to accomplish tasks, it's recommended that you give them a name to make it easier to manage them:
mng create my-task
# that command give the agent a name of "my-task". If you don't specify a name, mng will generate a random one for you.

# you can use a short form for most commands (like create) as well--the above command is the same as these:
mng create my-task claude
mng c my-task

# for the rest of this doc, we'll use the explicit form (specifying "create") just to be extra clear,
# but you might want to use the short form in your day-to-day work for speed and convenience.
# you can (and should) create aliases and templated as well (see TEMPLATES, ALIASES, AND SHORTCUTS below)

# you can also specify a different agent (ex: codex)
mng create my-task codex

# you can specify the arguments to the *agent* (ie, send args to claude rather than mng)
# by using `--` to separate the agent arguments from the mng arguments:
mng create my-task -- --model opus
# that command launches claude with the "opus" model instead of the default

# you can also launch claude remotely in Modal:
mng create my-task --in modal
# see more details below in "CREATING AGENTS REMOTELY" for relevant options

# you can run *any* literal command instead of a named agent type:
mng create my-task --agent-command python -- my_script.py
# remember that the arguments to the "agent" (or command) come after the `--` separator

# this enables some pretty interesting use cases, like running servers or other programs (besides AI agents)
# this make debugging easy--you can snapshot when a task is complete, then later connect to that exact machine state:
mng create my-task --agent-command python --idle-mode run --idle-timeout 60 -- my_long_running_script.py extra-args
# see "RUNNING NON-AGENT PROCESSES" below for more details

# alternatively, you can simply add extra tmux windows that run alongside your agent:
mng create my-task --add-command server="npm run dev" --add-command logs="tail -f app.log"
# that command automatically starts two tmux windows named "server" and "logs" that run those commands (in addition to the main window that runs the agent)

## SENDING MESSAGES ON LAUNCH

# you can send an initial message (so you don't have to wait around, eg, while a Modal container starts)
mng create my-task --in modal --no-connect --message "Speed up one of my tests and make a PR on github"
# here we disable the default --connect behavior (because presumably you just wanted to launch that in the background and continue on your way)
# and then we also pass in an explicit message for the agent to start working on immediately
# the message can also be specified as the contents of a file (by using --message-file instead of --message)

# you can also edit the message *while the agent is starting up*, which is very handy for making it "feel" instant:
mng create my-task --in modal --edit-message

## SPECIFYING DATA FOR THE AGENT

# by default, the agent uses the data from its current git repo (if any) or folder, but you can specify a different source:
mng create my-task --context /path/to/some/other/project

# similarly, by default the agent is tagged with a "project" label that matches the name of the current git repo (or folder), but you can specify a different project:
mng create my-task --project my-project

# mng doesn't require git at all--if there's no git repo, it will just use the files from the folder as context.
mkdir -p /tmp/my_random_folder
echo "print('hello world')" > /tmp/my_random_folder/script.py
mng create my-task --context /tmp/my_random_folder --agent-command python -- script.py

# however, if you do use git, mng makes that convenient
# by default, it creates a new git branch for each agent (so that their changes don't conflict with each other):
mng create my-task
git branch | grep mng/my-task

# --new-branch-prefix controls the prefix for auto-generated branch names (default: mng/)
mng create my-task --new-branch-prefix "feature/"
git branch | grep feature/my-task

# you can also specify a different base branch (instead of the current branch):
mng create my-task --base-branch main

# or set the new branch name explicitly:
mng create my-task --new-branch feature/my-task

# you can create a copy instead of a worktree:
mng create my-task --copy
# that is used by default if you're not in a git repo

# you can disable new branch creation entirely with --no-new-branch (requires --in-place or --copy due to how worktrees work, and --in-place implies --no-new-branch):
mng create my-task --copy --no-new-branch

# you can create a "clone" instead of worktree or copy, which is a lightweight copy that shares git objects with the original repo but has its own separate working directory:
mng create my-task --clone

# you can make a shallow clone for faster setup:
mng create my-task --depth 1
# (--shallow-since clones since a specific date instead)

# you can clone from an existing agent's work directory:
mng create my-task --from other-agent
# (--source, --source-agent, and --source-host are alternative forms for more specific control)

# you can use rsync to transfer extra data as well, beyond just the git data:
mng create my-task --in modal --rsync --rsync-args "--exclude=node_modules"

## CREATING AGENTS REMOTELY

# one of the coolest features of mng is the ability to create agents on remote hosts just as easily as you can create them locally:
mng create my-task --in modal -- --dangerously-skip-permissions --append-system-prompt "Don't ask me any questions!"
# that command passes the "--dangerously-skip-permissions" flag to claude because it's safe to do so:
# agents running remotely are running in a sandboxed environment where they can't really mess anything up on their local machine (or if they do, it doesn't matter)
# because it's running remotely, you might also want something like that system prompt (to tell it not to get blocked on you)

# running agents remotely is really cool because you can create an unlimited number of them, but it comes with some downsides
# one of the main downsides is cost--remote hosts aren't free, and if you forget about them, they can rack up a big bill.
# mng makes it really easy to deal with this by automatically shutting down hosts when their agents are idle:
mng create my-task --in modal --idle-timeout 60
# that command shuts down the Modal host (and agent) after 1 minute of inactivity.

# You can customize what "inactivity" means by using the --idle-mode flag:
mng create my-task --in modal --idle-mode "ssh"
# that command will only consider agents as "idle" when you are not connected to them
# see the idle_detection.md file for more details on idle detection and timeouts

# you can specify which existing host to run on (eg, if you have multiple Modal hosts or SSH servers):
mng create my-task --host my-dev-box
# (--target-host is an alternative, more explicit form)

# generally though, you'll want to construct a new Modal host for each agent.
# build arguments let you customize that new remote host (eg, GPU type, memory, base Docker image for Modal):
mng create my-task --in modal --build-arg cpu=4 --build-arg memory=16 --build-arg image=python:3.12
# (-b is an alternative forms of --build-arg; see "mng create --help" for all provider-specific build args)
# some other useful Modal build args: --region, --timeout, --offline (blocks network), --secret, --cidr-allowlist, --context-dir

# the most important build args for Modal are probably "--file" and "--context-dir",
# which let you specify a custom Dockerfile and build context directory (respectively) for building the host environment.
# This is how you can get custom dependencies, files, and setup steps on your Modal hosts. For example:
mng create my-task --in modal --build-args "file=./Dockerfile.agent context-dir=./agent-context"
# that command builds a Modal host using the Dockerfile at ./Dockerfile.agent and the build context at ./agent-context
# (which is where the Dockerfile can COPY files from, and also where build args are evaluated from)
# that command also demonstrates how to pass multiple build args in a single --build-args string (instead of using multiple --build-arg flags)

# you can name the host separately from the agent:
mng create my-task --in modal --host-name my-modal-box
# (--host-name-style and --name-style control auto-generated name styles for hosts and agents respectively)

# you can mount persistent Modal volumes in order to share data between hosts, or have it be available even when they are offline (or after they are destroyed):
mng create my-task --in modal --build-arg volume=my-data:/data

# you can use an existing snapshot instead of building a new host from scratch:
mng create my-task --in modal --snapshot snap-123abc

# some providers (like docker), take "start" args as well as build args:
mng create my-task --in docker --start-arg "--gpus all"
# these args are passed to "docker run", whereas the build args are passed to "docker build".

# you can specify the target path where the agent's work directory will be mounted:
mng create my-task --in modal --target-path /workspace

# you can upload files and run custom commands during host provisioning:
mng create my-task --in modal --upload-file ~/.ssh/config:/root/.ssh/config --user-command "pip install foo"
# (--sudo-command runs as root; --append-to-file, --prepend-to-file, and --create-directory are also available)

# you can add SSH known hosts for outbound SSH from the agent:
mng create my-task --in modal --known-host "github.com ssh-ed25519 AAAA..."
# that is particularly helpful when creating agents that you want to share with other people or other installations of mng, since they won't have your local machine's keys automatically
# it can also be useful for setting up automations in CI (so that you can access them later)

# by default, agents are started when a host is booted. This can be disabled:
mng create my-task --in modal --no-start-on-boot
# but it only makes sense to do this if you are running multiple agents on the same host
# that's because hosts are automatically stopped when they have no more running agents, so you have to have at least one.

## CONTROLLING THE AGENT ENVIRONMENT

# you can set environment variables for the agent:
mng create my-task --env DEBUG=true
# (--env-file loads from a file, --pass-env forwards a variable from your current shell)

# it is *strongly encouraged* to use either use --env-file or --pass-env, especially for any sensitive environment variables (like API keys) rather than --env, because that way they won't end up in your shell history or in your config files by accident. For example:
export API_KEY=abc123
mng create my-task --pass-env API_KEY
# that command passes the API_KEY environment variable from your current shell into the agent's environment, without you having to specify the value on the command line.

# you can also set host-level environment variables (separate from agent env vars):
mng create my-task --in modal --pass-host-env MY_VAR
# --host-env-file and --pass-host-env work the same as their agent counterparts, and again, you should generally prefer those forms (but if you really need to you can use --host-env to specify host env vars directly)

## TEMPLATES, ALIASES, AND SHORTCUTS

# you can use templates to quickly apply a set of preconfigured options:
echo '[create_templates.my_modal_template]' >> .mng/settings.local.toml
echo 'new_host = "modal"' >> .mng/settings.local.toml
echo 'build_args = "cpu=4"' >> .mng/settings.local.toml
mng create my-task --template my_modal_template
# templates are defined in your config (see the CONFIGURATION section for more) and can be stacked: --template modal --template codex
# templates take exactly the same parameters as the create command
# -t is short for --template. Many commands have a short form (see the "--help")

# you can enable or disable specific plugins:
mng create my-task --plugin my-plugin --disable-plugin other-plugin

# you should probably use aliases for making little shortcuts for yourself, because many of the commands can get a bit long:
echo "alias mc='mng create --in-place'" >> ~/.bashrc && source ~/.bashrc
# or use a more sophisticated tool, like Espanso

## TIPS AND TRICKS

# by default, mng aborts the create command if the working tree has uncommitted changes. You can avoid this by doing:
mng create my-task --no-ensure-clean
# this is particularly useful for starting agents when, eg, you are in the middle of a merge conflict and you just want the agent to finish it off, for example
# it should probably be avoided in general, because it makes it more difficult to merge work later.

# another handy trick is to make the create command "idempotent" so that you don't need to worry about remembering whether you created an agent yet or not:
mng create sisyphus --reuse --in modal
# if that agent already exists, it will be reused (and started) instead of creating a new one. If it doesn't exist, it will be created.

# you can control connection retries and timeouts:
mng create my-task --in modal --retry 5 --retry-delay 10s --ready-timeout 30
# (--reconnect / --no-reconnect controls auto-reconnect on disconnect)

# you can use a custom connect command instead of the default (eg, useful for, say, connecting in a new iterm window instead of the current one)
mng create my-task --connect-command "my_script.sh"

# you can add labels to organize your agents and tags for host metadata:
mng create my-task --label team=backend --tag env=staging

## CREATING AND USING AGENTS PROGRAMMATICALLY

# mng is very much meant to be used for scripting and automation, so nothing requires interactivity.
# if you want to be sure that interactivity is disabled, you can use the --headless flag: [future]
mng create my-task --headless

# or you can set that option in your config so that it always applies: [future]
mng config set headless True

# or you can set it as an environment variable: [future]
export MNG_HEADLESS=True

# *all* mng options work like that. For example, if you want to always run agents in Modal by default, you can set that in your config:
mng config set commands.create.in modal
# for more on configuration, see the CONFIGURATION section below

# you can control output format for scripting:
mng create my-task --no-connect --format json
# (--json and --jsonl are shorthands; --quiet suppresses all output)

# you can wait for the agent to finish before the command returns (great for scripting):
mng create my-task --no-connect --await-agent-stopped --message "Do the thing"
# (--await-ready waits only until the agent is ready, not until it finishes)

# you can send a message when resuming a stopped agent. This is very useful for making more robust agents (eg, that can resume after crashing or being interrupted)
mng create my-task --resume-message "Continue where you left off"
# (--resume-message-file reads the resume message from a file)

## LEARNING MORE

# tons more arguments for anything you could want! As always, you can learn more via --help
mng create --help

# or see the other commands--list, destroy, message, connect, push, pull, clone, and more!  These other commands are covered in their own sections below.
mng --help

##############################################################################
# LISTING AGENTS
#   After you've created a bunch of agents, you might lose track of them! So "mng list" makes it easy to see all of your agents,
#   as well as any important information about them (ex: where they're running, when they were last active, etc.)
##############################################################################

##############################################################################
# CONNECTING TO AGENTS
#   If you've disconnected from an agent (or created one with --no-connect),
#   you can reconnect to it at any time.
##############################################################################


##############################################################################
# SENDING MESSAGES TO AGENTS
#   You can send messages to running agents without connecting to them.
#   This is useful for giving agents new instructions while they work.
##############################################################################


##############################################################################
# EXECUTING COMMANDS ON AGENTS
#   Run shell commands on an agent's host without connecting interactively.
#   Useful for scripting, checking status, or running one-off operations.
##############################################################################


##############################################################################
# OPENING AGENTS IN THE BROWSER
#   Some agents expose web interfaces. "mng open" launches them in your
#   browser, so you can interact with agents visually.
##############################################################################


##############################################################################
# PUSHING FILES TO AGENTS
#   Push local files or git commits to a running agent. This is how you
#   sync your local changes to an agent's workspace.
##############################################################################


##############################################################################
# PULLING FILES FROM AGENTS
#   Pull files or git commits from an agent back to your local machine.
#   This is how you retrieve an agent's work.
##############################################################################


##############################################################################
# PAIRING WITH AGENTS
#   Continuously sync files between your local machine and an agent in
#   real time. Great for working alongside an agent on the same codebase.
##############################################################################


##############################################################################
# STARTING AND STOPPING AGENTS
#   Stopped agents can be restarted, and running agents can be stopped to
#   free resources. Stopping can optionally create a snapshot for later.
##############################################################################


##############################################################################
# RENAMING AGENTS
#   Rename an agent to something more descriptive, or to avoid name
#   collisions.
##############################################################################


##############################################################################
# DESTROYING AGENTS
#   When you're done with an agent, destroy it to clean up all of its
#   resources (host, snapshots, volumes, etc.).
##############################################################################


##############################################################################
# CLONING AND MIGRATING AGENTS
#   Clone an agent to create a copy of it (on the same or different host),
#   or migrate an agent to move it to a different host entirely.
##############################################################################


##############################################################################
# MANAGING SNAPSHOTS
#   Snapshots capture the filesystem state of a host. You can create, list,
#   and destroy them, and use them to restore or fork agents.
##############################################################################


##############################################################################
# PROVISIONING AGENTS
#   Re-run provisioning steps on an existing agent, such as installing
#   packages, uploading files, or running setup commands.
##############################################################################


##############################################################################
# MANAGING AGENT LIMITS
#   Configure idle timeouts, activity tracking, permissions, and other
#   runtime limits for agents and hosts.
##############################################################################


##############################################################################
# CLEANING UP RESOURCES
#   Bulk-destroy or stop agents based on filters like age, idle time, or
#   provider. Also garbage-collect unused resources like orphaned snapshots
#   and volumes.
##############################################################################


##############################################################################
# VIEWING LOGS
#   View log files for agents and hosts. Useful for debugging and
#   monitoring what your agents are up to.
##############################################################################


##############################################################################
# MANAGING PLUGINS
#   List, enable, and disable plugins that extend mng with new agent types,
#   provider backends, and CLI commands.
##############################################################################


##############################################################################
# CONFIGURATION
#   Customize mng's behavior via configuration files. Set defaults for
#   commands, define create templates, and configure providers.
##############################################################################


##############################################################################
# COMMON TASKS
#   Quick recipes for the things you'll do most often: launching an agent
#   on a task, checking on it, grabbing its work, and cleaning up after.
##############################################################################


##############################################################################
# PROJECTS
#   Agents are automatically associated with a project (the git repo you
#   run mng from). Use projects to organize agents and filter your list.
##############################################################################


##############################################################################
# MULTI-AGENT WORKFLOWS
#   Run multiple agents in parallel on different tasks, coordinate their
#   work, and bring everything together.
##############################################################################


##############################################################################
# WORKING WITH GIT
#   Push and pull git commits (not just files) between your machine and
#   agents. Branch management, merge strategies, and worktree support.
##############################################################################


##############################################################################
# LABELS AND FILTERING
#   Tag agents with labels and use CEL filter expressions to target
#   specific agents across list, destroy, cleanup, and other commands.
##############################################################################


##############################################################################
# CREATE TEMPLATES
#   Define reusable presets that bundle common options (provider, build
#   args, permissions, environment, etc.) into a single template name.
##############################################################################


##############################################################################
# CUSTOM AGENT TYPES
#   Define your own agent types in config, or use any command in your PATH
#   as an agent. Wrap existing tools with custom defaults and permissions.
##############################################################################


##############################################################################
# ENVIRONMENT VARIABLES
#   Pass environment variables to agents during creation, control mng
#   behavior via env vars, and understand the variables mng sets for you.
##############################################################################


##############################################################################
# RUNNING AGENTS ON MODAL
#   Launch agents in Modal sandboxes for full isolation, GPU access, and
#   cloud-based execution. Custom images, secrets, volumes, and networking.
##############################################################################


##############################################################################
# RUNNING AGENTS IN DOCKER
#   Run agents in Docker containers for local isolation without cloud
#   costs. Good for untrusted code or reproducible environments.
##############################################################################


##############################################################################
# RUNNING AGENTS LOCALLY
#   The simplest and fastest option. Agents run directly on your machine
#   with no isolation overhead. Best for trusted agents and quick tasks.
##############################################################################


##############################################################################
# IDLE DETECTION AND TIMEOUTS
#   Automatically pause or stop agents when they go idle to save resources.
#   Configure what counts as "activity" and how long to wait.
##############################################################################


##############################################################################
# PERMISSIONS
#   Grant agents specific capabilities (like network access or filesystem
#   writes) and revoke them. Permissions are enforced by plugins.
##############################################################################


##############################################################################
# MULTIPLE AGENTS ON ONE HOST
#   Run several agents on the same host to share resources and reduce
#   costs. Agents share the host filesystem and network.
##############################################################################


##############################################################################
# RUNNING NON-AGENT PROCESSES
#   mng is useful for more than just AI agents! Run any long-lived process (like servers, data pipelines, etc.)
#   with mng to get the same benefits of easy management, logging, and remote execution.
##############################################################################


##############################################################################
# SCRIPTING AND AUTOMATION
#   Use mng in shell scripts, CI pipelines, and cron jobs. JSON output,
#   headless mode, idempotent creation, and programmatic control.
##############################################################################


##############################################################################
# OUTPUT FORMATS AND MACHINE-READABLE OUTPUT
#   Switch between human-readable, JSON, and JSONL output. Use --format
#   with templates, pipe output to jq, and build tooling on top of mng.
##############################################################################


##############################################################################
# DEVCONTAINER HOOKS
#   Use devcontainer lifecycle hooks (onCreateCommand, postStartCommand,
#   etc.) to customize agent environments during provisioning.
##############################################################################


##############################################################################
# UPLOADING FILES AND RUNNING SETUP COMMANDS
#   Upload files, append to configs, create directories, and run setup
#   commands on agent hosts during creation or via re-provisioning.
##############################################################################


##############################################################################
# TROUBLESHOOTING
#   Common problems and how to fix them. Debugging with logs, verbose
#   output, and exec. What to do when agents crash or hosts won't start.
##############################################################################


##############################################################################
# TIPS AND TRICKS
#   Power-user shortcuts, lesser-known features, and workflow patterns
#   that make working with mng faster and more pleasant.
##############################################################################

