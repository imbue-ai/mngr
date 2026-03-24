#!/usr/bin/env bash
set -euo pipefail
# This is a (very long) tutorial that covers most of the features of mng with examples, as well as simple ways to do common tasks.
# See the README.md for details on installation and higher level architecture.

##############################################################################
# CREATING AGENTS
#   One of the most common things you'll want to do with mng is create agents. There are *tons* of options,
#   so basically any workflow you want should be supported.
##############################################################################

## BASIC CREATION

# running mng create is strictly better than running claude! It's less letters to type :-D
# running this command launches claude (Claude Code) immediately *in a new worktree*
mng create
# the defaults are the following: agent=claude, provider=local, project=current dir

# if you want the default behavior of claude (starting in-place), you can specify that:
mng create --in-place
# mng defaults to creating a new worktree for each agent because the whole point of mng is to let you run multiple agents in parallel.
# without creating a new worktree for each, they will make conflicting changes with one another.

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
mng create my-task --provider modal
# see more details below in "CREATING AGENTS REMOTELY" for relevant options

# you can run *any* literal command instead of a named agent type:
mng create my-task --command python -- my_script.py
# remember that the arguments to the "agent" (or command) come after the `--` separator

# this enables some pretty interesting use cases, like running servers or other programs (besides AI agents)
# this make debugging easy--you can snapshot when a task is complete, then later connect to that exact machine state:
mng create my-task --command python --idle-mode run --idle-timeout 60 -- my_long_running_script.py extra-args
# see "RUNNING NON-AGENT PROCESSES" below for more details

# alternatively, you can simply add extra tmux windows that run alongside your agent:
mng create my-task -w server="npm run dev" -w logs="tail -f app.log"
# that command automatically starts two tmux windows named "server" and "logs" that run those commands (in addition to the main window that runs the agent)

## SENDING MESSAGES ON LAUNCH

# you can send an initial message (so you don't have to wait around, eg, while a Modal container starts)
mng create my-task --provider modal --no-connect --message "Speed up one of my tests and make a PR on github"
# here we disable the default --connect behavior (because presumably you just wanted to launch that in the background and continue on your way)
# and then we also pass in an explicit message for the agent to start working on immediately
# the message can also be specified as the contents of a file (by using --message-file instead of --message)

# you can also edit the message *while the agent is starting up*, which is very handy for making it "feel" instant:
mng create my-task --provider modal --edit-message

## SPECIFYING DATA FOR THE AGENT

# by default, the agent uses the data from its current git repo (if any) or folder, but you can specify a different source:
mng create my-task --source-path /path/to/some/other/project

# similarly, by default the agent is tagged with a "project" label that matches the name of the current git repo (or folder), but you can specify a different project:
mng create my-task --project my-project

# mng doesn't require git at all--if there's no git repo, it will just use the files from the folder as the source data
mkdir -p /tmp/my_random_folder
echo "print('hello world')" > /tmp/my_random_folder/script.py
mng create my-task --source-path /tmp/my_random_folder --command python -- script.py

# however, if you do use git, mng makes that convenient
# by default, it creates a new git branch for each agent (so that their changes don't conflict with each other):
mng create my-task
git branch | grep mng/my-task

# --branch controls branch creation. the default is :mng/* which creates a new branch named mng/{agent_name}
# you can change the pattern (the * is replaced by the agent name):
mng create my-task --branch ":feature/*"
git branch | grep feature/my-task

# you can also specify a different base branch (instead of the current branch):
mng create my-task --branch "main:mng/*"

# or set the new branch name explicitly:
mng create my-task --branch ":feature/my-task"

# you can create a copy instead of a worktree:
mng create my-task --copy
# that is used by default if you're not in a git repo

# you can disable new branch creation entirely by omitting the :NEW part (requires --in-place or --copy due to how worktrees work, and --in-place implies no new branch):
mng create my-task --copy --branch main

# you can create a "clone" instead of worktree or copy, which is a lightweight copy that shares git objects with the original repo but has its own separate working directory:
mng create my-task --clone

# you can make a shallow clone for faster setup:
mng create my-task --depth 1
# (--shallow-since clones since a specific date instead)

# you can clone from an existing agent's work directory:
mng create my-task --from other-agent
# (--source, --source-agent, and --source-host are alternative forms for more specific control)

# you can use rsync to transfer extra data as well, beyond just the git data:
mng create my-task --provider modal --rsync --rsync-args "--exclude=node_modules"

## CREATING AGENTS REMOTELY

# one of the coolest features of mng is the ability to create agents on remote hosts just as easily as you can create them locally:
mng create my-task --provider modal -- --dangerously-skip-permissions --append-system-prompt "Don't ask me any questions!"
# that command passes the "--dangerously-skip-permissions" flag to claude because it's safe to do so:
# agents running remotely are running in a sandboxed environment where they can't really mess anything up on their local machine (or if they do, it doesn't matter)
# because it's running remotely, you might also want something like that system prompt (to tell it not to get blocked on you)

# running agents remotely is really cool because you can create an unlimited number of them, but it comes with some downsides
# one of the main downsides is cost--remote hosts aren't free, and if you forget about them, they can rack up a big bill.
# mng makes it really easy to deal with this by automatically shutting down hosts when their agents are idle:
mng create my-task --provider modal --idle-timeout 60
# that command shuts down the Modal host (and agent) after 1 minute of inactivity.

# You can customize what "inactivity" means by using the --idle-mode flag:
mng create my-task --provider modal --idle-mode "ssh"
# that command will only consider agents as "idle" when you are not connected to them
# see the idle_detection.md file for more details on idle detection and timeouts

# you can specify which existing host to run on using the address syntax (eg, if you have multiple Modal hosts or SSH servers):
mng create my-task@my-dev-box

# generally though, you'll want to construct a new Modal host for each agent.
# build arguments let you customize that new remote host (eg, GPU type, memory, base Docker image for Modal):
mng create my-task --provider modal -b cpu=4 -b memory=16 -b image=python:3.12
# see "mng create --help" for all provider-specific build args
# some other useful Modal build args: --region, --timeout, --offline (blocks network), --secret, --cidr-allowlist, --context-dir

# the most important build args for Modal are probably "--file" and "--context-dir",
# which let you specify a custom Dockerfile and build context directory (respectively) for building the host environment.
# This is how you can get custom dependencies, files, and setup steps on your Modal hosts. For example:
mng create my-task --provider modal -b file=./Dockerfile.agent -b context-dir=./agent-context
# that command builds a Modal host using the Dockerfile at ./Dockerfile.agent and the build context at ./agent-context
# (which is where the Dockerfile can COPY files from, and also where build args are evaluated from)

# you can name the host using the address syntax:
mng create my-task@my-modal-box.modal --new-host
# (--host-name-style and --name-style control auto-generated name styles for hosts and agents respectively)

# you can mount persistent Modal volumes in order to share data between hosts, or have it be available even when they are offline (or after they are destroyed):
mng create my-task --provider modal -b volume=my-data:/data

# you can use an existing snapshot instead of building a new host from scratch:
mng create my-task --provider modal --snapshot snap-123abc

# some providers (like docker), take "start" args as well as build args:
mng create my-task --provider docker -s "--gpus all"
# these args are passed to "docker run", whereas the build args are passed to "docker build".

# you can specify the target path where the agent's work directory will be mounted:
mng create my-task --provider modal --target-path /workspace

# you can upload files and run custom commands during host provisioning:
mng create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config --user-command "pip install foo"
# (--sudo-command runs as root; --append-to-file and --prepend-to-file are also available)

# by default, agents are started when a host is booted. This can be disabled:
mng create my-task --provider modal --no-start-on-boot
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
mng create my-task --provider modal --pass-host-env MY_VAR
# --host-env-file and --pass-host-env work the same as their agent counterparts, and again, you should generally prefer those forms (but if you really need to you can use --host-env to specify host env vars directly)

## TEMPLATES, ALIASES, AND SHORTCUTS

# you can use templates to quickly apply a set of preconfigured options:
echo '[create_templates.my_modal_template]' >> .mng/settings.local.toml
echo 'provider = "modal"' >> .mng/settings.local.toml
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
mng create sisyphus --reuse --provider modal
# if that agent already exists, it will be reused (and started) instead of creating a new one. If it doesn't exist, it will be created.

# you can control connection retries and timeouts:
mng create my-task --provider modal --retry 5 --retry-delay 10s
# (--reconnect / --no-reconnect controls auto-reconnect on disconnect)

# you can use a custom connect command instead of the default (eg, useful for, say, connecting in a new iterm window instead of the current one)
mng create my-task --connect-command "my_script.sh"

# you can add labels to organize your agents and tags for host metadata:
mng create my-task --label team=backend --host-label env=staging

## CREATING AND USING AGENTS PROGRAMMATICALLY

# mng is very much meant to be used for scripting and automation, so nothing requires interactivity.
# if you want to be sure that interactivity is disabled, you can use the --headless flag:
mng create my-task --headless

# or you can set that option in your config so that it always applies:
mng config set headless true

# or you can set it as an environment variable:
export MNG_HEADLESS=true

# *all* mng options work like that. For example, if you want to always run agents in Modal by default, you can set that in your config:
mng config set commands.create.provider modal
# for more on configuration, see the CONFIGURATION section below

# you can control output format for scripting:
mng create my-task --no-connect --format json
# (--quiet suppresses all output)

# you can send a message when starting the agent (great for scripting):
mng create my-task --no-connect --message "Do the thing"

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


# auto-generated by Claude, remove when a human has sanctioned this
# list all agents
mng list

# auto-generated by Claude, remove when a human has sanctioned this
# short form
mng ls

# auto-generated by Claude, remove when a human has sanctioned this
# show only running agents
mng ls --running

# auto-generated by Claude, remove when a human has sanctioned this
# show only stopped agents
mng ls --stopped

# auto-generated by Claude, remove when a human has sanctioned this
# show only agents running locally vs remotely
mng ls --local
mng ls --remote

# auto-generated by Claude, remove when a human has sanctioned this
# filter by provider
mng ls --provider modal

# auto-generated by Claude, remove when a human has sanctioned this
# filter by project
mng ls --project my-project

# auto-generated by Claude, remove when a human has sanctioned this
# choose which fields to display and sort order
mng ls --fields "name,state,host.provider,created_at" --sort "-created_at"

# auto-generated by Claude, remove when a human has sanctioned this
# limit the number of results
mng ls --limit 10

# auto-generated by Claude, remove when a human has sanctioned this
# watch mode: refresh the list every 5 seconds
mng ls --watch 5

# auto-generated by Claude, remove when a human has sanctioned this
# output as JSON for scripting
mng ls --format json

# auto-generated by Claude, remove when a human has sanctioned this
# stream results as JSONL (useful for piping to jq)
mng ls --stream --format jsonl

##############################################################################
# CONNECTING TO AGENTS
#   If you've disconnected from an agent (or created one with --no-connect),
#   you can reconnect to it at any time.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# connect to a running agent by name
mng connect my-task

# auto-generated by Claude, remove when a human has sanctioned this
# short form
mng conn my-task

# auto-generated by Claude, remove when a human has sanctioned this
# connect and start the agent if it's stopped
mng connect my-task --start

# auto-generated by Claude, remove when a human has sanctioned this
# connect without auto-starting (fails if agent is stopped)
mng connect my-task --no-start

##############################################################################
# SENDING MESSAGES TO AGENTS
#   You can send messages to running agents without connecting to them.
#   This is useful for giving agents new instructions while they work.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# send a message to a specific agent
mng message my-task -m "Please also add unit tests for the new function"

# auto-generated by Claude, remove when a human has sanctioned this
# short form
mng msg my-task -m "Check the CI results and fix any failures"

# auto-generated by Claude, remove when a human has sanctioned this
# send the same message to multiple agents by name
mng msg agent-1 agent-2 agent-3 -m "Wrap up and commit your changes"

# auto-generated by Claude, remove when a human has sanctioned this
# send a message to all agents
mng msg -a -m "Stop what you are doing and commit your current progress"

# auto-generated by Claude, remove when a human has sanctioned this
# send a message to agents matching a filter
mng msg --include 'host.provider == "modal"' -m "Almost out of budget, please finish up"

# auto-generated by Claude, remove when a human has sanctioned this
# control error handling when messaging multiple agents
mng msg -a -m "Status update please" --on-error continue

##############################################################################
# EXECUTING COMMANDS ON AGENTS
#   Run shell commands on an agent's host without connecting interactively.
#   Useful for scripting, checking status, or running one-off operations.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# run a command on a specific agent's host
mng exec my-task -- ls -la /workspace

# auto-generated by Claude, remove when a human has sanctioned this
# short form
mng x my-task -- git status

# auto-generated by Claude, remove when a human has sanctioned this
# run a command on all agents
mng x -a -- whoami

# auto-generated by Claude, remove when a human has sanctioned this
# run a command as a specific user
mng x my-task --user root -- apt-get update

# auto-generated by Claude, remove when a human has sanctioned this
# run a command in a specific working directory
mng x my-task --cwd /tmp -- pwd

# auto-generated by Claude, remove when a human has sanctioned this
# set a timeout (in seconds) for the command
mng x my-task --timeout 30 -- python long_script.py

# auto-generated by Claude, remove when a human has sanctioned this
# start the agent's host if it's stopped, run the command, then leave it running
mng x my-task --start -- cat /etc/os-release

# auto-generated by Claude, remove when a human has sanctioned this
# control error handling when running on multiple agents
mng x -a --on-error continue -- git log --oneline -5

##############################################################################
# PUSHING FILES TO AGENTS
#   Push local files or git commits to a running agent. This is how you
#   sync your local changes to an agent's workspace.
##############################################################################

# "push" is an experimental command. See "mng push --help" for current usage.

##############################################################################
# PULLING FILES FROM AGENTS
#   Pull files or git commits from an agent back to your local machine.
#   This is how you retrieve an agent's work.
##############################################################################

# "pull" is an experimental command. See "mng pull --help" for current usage.

##############################################################################
# PAIRING WITH AGENTS
#   Continuously sync files between your local machine and an agent in
#   real time. Great for working alongside an agent on the same codebase.
##############################################################################

# "pair" is an experimental command. See "mng pair --help" for current usage.

##############################################################################
# STARTING AND STOPPING AGENTS
#   Stopped agents can be restarted, and running agents can be stopped to
#   free resources. Stopping can optionally create a snapshot for later.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# start a stopped agent
mng start my-task

# auto-generated by Claude, remove when a human has sanctioned this
# start a stopped agent and immediately connect to it
mng start my-task --connect

# auto-generated by Claude, remove when a human has sanctioned this
# start multiple agents at once
mng start agent-1 agent-2 agent-3

# auto-generated by Claude, remove when a human has sanctioned this
# start all stopped agents
mng start -a

# auto-generated by Claude, remove when a human has sanctioned this
# dry-run to see what would happen without actually starting anything
mng start -a --dry-run

# auto-generated by Claude, remove when a human has sanctioned this
# stop a running agent
mng stop my-task

# auto-generated by Claude, remove when a human has sanctioned this
# short form
mng s my-task

# auto-generated by Claude, remove when a human has sanctioned this
# stop and archive the agent (creates a snapshot before stopping)
mng stop my-task --archive

# auto-generated by Claude, remove when a human has sanctioned this
# stop all running agents
mng stop -a

# auto-generated by Claude, remove when a human has sanctioned this
# dry-run to see what would be stopped
mng stop -a --dry-run

##############################################################################
# DESTROYING AGENTS
#   When you're done with an agent, destroy it to clean up all of its
#   resources (host, snapshots, volumes, etc.).
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# destroy a specific agent
mng destroy my-task

# auto-generated by Claude, remove when a human has sanctioned this
# short form
mng rm my-task

# auto-generated by Claude, remove when a human has sanctioned this
# destroy without confirmation prompt
mng rm my-task --force

# auto-generated by Claude, remove when a human has sanctioned this
# destroy and also remove the git branch that was created for the agent
mng rm my-task --force --remove-created-branch

# auto-generated by Claude, remove when a human has sanctioned this
# destroy multiple agents at once
mng rm agent-1 agent-2 agent-3 --force

# auto-generated by Claude, remove when a human has sanctioned this
# destroy all agents (be careful!)
mng rm -a --force

# auto-generated by Claude, remove when a human has sanctioned this
# dry-run to see what would be destroyed without actually doing it
mng rm -a --dry-run

# auto-generated by Claude, remove when a human has sanctioned this
# destroy and run garbage collection afterward
mng rm my-task --force --gc

##############################################################################
# CLONING AND MIGRATING AGENTS
#   Clone an agent to create a copy of it (on the same or different host),
#   or migrate an agent to move it to a different host entirely.
##############################################################################

# "clone" is an experimental command. See "mng clone --help" for current usage.
# "migrate" is an experimental command. See "mng migrate --help" for current usage.

##############################################################################
# MANAGING SNAPSHOTS
#   Snapshots capture the filesystem state of a host. You can create, list,
#   and destroy them, and use them to restore or fork agents.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# create a snapshot of an agent's host
mng snapshot create my-task

# auto-generated by Claude, remove when a human has sanctioned this
# short form
mng snap create my-task

# auto-generated by Claude, remove when a human has sanctioned this
# create a snapshot with a descriptive name
mng snap create my-task --name "before-refactor"

# auto-generated by Claude, remove when a human has sanctioned this
# snapshot all agents' hosts
mng snap create -a

# auto-generated by Claude, remove when a human has sanctioned this
# dry-run to see what would be snapshotted
mng snap create my-task --dry-run

# auto-generated by Claude, remove when a human has sanctioned this
# list all snapshots
mng snap list

# auto-generated by Claude, remove when a human has sanctioned this
# list snapshots for a specific agent's host
mng snap list my-task

# auto-generated by Claude, remove when a human has sanctioned this
# limit the number of snapshots shown
mng snap list --limit 5

# auto-generated by Claude, remove when a human has sanctioned this
# destroy a specific snapshot
mng snap destroy my-task --snapshot snap-123abc

# auto-generated by Claude, remove when a human has sanctioned this
# destroy all snapshots for an agent's host
mng snap destroy my-task --all-snapshots --force

# auto-generated by Claude, remove when a human has sanctioned this
# dry-run to see what would be destroyed
mng snap destroy my-task --all-snapshots --dry-run

##############################################################################
# PROVISIONING AGENTS
#   Re-run provisioning steps on an existing agent, such as installing
#   packages, uploading files, or running setup commands.
##############################################################################

# "provision" is an experimental command. See "mng provision --help" for current usage.

##############################################################################
# RENAMING AGENTS
#   Rename an agent to something more descriptive, or to avoid name
#   collisions.
##############################################################################

# "rename" is an experimental command. See "mng rename --help" for current usage.

##############################################################################
# MANAGING AGENT LIMITS
#   Configure idle timeouts, activity tracking, permissions, and other
#   runtime limits for agents and hosts.
##############################################################################

# "limit" is an experimental command. See "mng limit --help" for current usage.

##############################################################################
# CLEANING UP RESOURCES
#   Bulk-destroy or stop agents based on filters like age, idle time, or
#   provider. Also garbage-collect unused resources like orphaned snapshots
#   and volumes.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# garbage collect all unused resources (dry-run first to see what would be cleaned)
mng gc --dry-run

# auto-generated by Claude, remove when a human has sanctioned this
# actually run garbage collection
mng gc

# auto-generated by Claude, remove when a human has sanctioned this
# clean up only specific resource types
mng gc --machines
mng gc --snapshots
mng gc --volumes
mng gc --work-dirs

# auto-generated by Claude, remove when a human has sanctioned this
# clean all agent resource types at once
mng gc --all-agent-resources

# auto-generated by Claude, remove when a human has sanctioned this
# clean up build cache and logs
mng gc --build-cache
mng gc --logs

# auto-generated by Claude, remove when a human has sanctioned this
# garbage collect for a specific provider only
mng gc --provider modal

# auto-generated by Claude, remove when a human has sanctioned this
# watch garbage collection progress (refresh every 5 seconds)
mng gc --watch 5

##############################################################################
# VIEWING EVENTS AND LOGS
#   View event stream and log files for agents and hosts. Useful for debugging and
#   monitoring what your agents are up to.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# view events for an agent
mng events my-task

# auto-generated by Claude, remove when a human has sanctioned this
# follow events in real time (like tail -f)
mng events my-task --follow

# auto-generated by Claude, remove when a human has sanctioned this
# show only the last 20 events
mng events my-task --tail 20

# auto-generated by Claude, remove when a human has sanctioned this
# show only the first 10 events
mng events my-task --head 10

# auto-generated by Claude, remove when a human has sanctioned this
# filter events using a CEL expression
mng events my-task --filter 'type == "state_change"'

# auto-generated by Claude, remove when a human has sanctioned this
# view the transcript of an agent's conversation
mng transcript my-task

# auto-generated by Claude, remove when a human has sanctioned this
# view only assistant messages
mng transcript my-task --role assistant

# auto-generated by Claude, remove when a human has sanctioned this
# view the last 5 messages
mng transcript my-task --tail 5

# auto-generated by Claude, remove when a human has sanctioned this
# output transcript as JSON for programmatic use
mng transcript my-task --format json

##############################################################################
# MANAGING PLUGINS
#   List, enable, and disable plugins that extend mng with new agent types,
#   provider backends, and CLI commands.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# list all available plugins
mng plugin list

# auto-generated by Claude, remove when a human has sanctioned this
# list only active plugins
mng plugin list --active

# auto-generated by Claude, remove when a human has sanctioned this
# list plugins with specific fields
mng plugin list --fields "name,version,active"

# auto-generated by Claude, remove when a human has sanctioned this
# add a plugin by name (from the registry)
mng plugin add my-plugin

# auto-generated by Claude, remove when a human has sanctioned this
# add a plugin from a local path
mng plugin add --path /path/to/my-plugin

# auto-generated by Claude, remove when a human has sanctioned this
# add a plugin from a git repository
mng plugin add --git https://github.com/user/mng-plugin.git

# auto-generated by Claude, remove when a human has sanctioned this
# remove a plugin
mng plugin remove my-plugin

# auto-generated by Claude, remove when a human has sanctioned this
# enable a plugin at the project scope
mng plugin enable my-plugin --scope project

# auto-generated by Claude, remove when a human has sanctioned this
# disable a plugin at the user scope
mng plugin disable my-plugin --scope user

##############################################################################
# CONFIGURATION
#   Customize mng's behavior via configuration files. Set defaults for
#   commands, define create templates, and configure providers.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# list all configuration values
mng config list

# auto-generated by Claude, remove when a human has sanctioned this
# list configuration at a specific scope (user, project, or local)
mng config list --scope user
mng config list --scope project
mng config list --scope local

# auto-generated by Claude, remove when a human has sanctioned this
# get a specific config value
mng config get commands.create.provider

# auto-generated by Claude, remove when a human has sanctioned this
# set a config value (at the default scope)
mng config set commands.create.provider modal

# auto-generated by Claude, remove when a human has sanctioned this
# set a config value at a specific scope
mng config set headless true --scope user

# auto-generated by Claude, remove when a human has sanctioned this
# unset a config value
mng config unset commands.create.provider

# auto-generated by Claude, remove when a human has sanctioned this
# open the config file in your editor
mng config edit

# auto-generated by Claude, remove when a human has sanctioned this
# open a specific scope's config file
mng config edit --scope project

# auto-generated by Claude, remove when a human has sanctioned this
# show the path to the config file
mng config path
mng config path --scope user

##############################################################################
# COMMON TASKS
#   Quick recipes for the things you'll do most often: launching an agent
#   on a task, checking on it, grabbing its work, and cleaning up after.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# Recipe: launch an agent on a task, check on it later, and clean up

# auto-generated by Claude, remove when a human has sanctioned this
# 1. Create an agent with a task, don't connect (let it work in the background)
mng create fix-bug --provider modal --no-connect --message "Fix the failing test in test_auth.py and make a PR"

# auto-generated by Claude, remove when a human has sanctioned this
# 2. Check what agents are running
mng ls --running

# auto-generated by Claude, remove when a human has sanctioned this
# 3. Check the agent's conversation to see its progress
mng transcript fix-bug --tail 3

# auto-generated by Claude, remove when a human has sanctioned this
# 4. Send a follow-up message if needed
mng msg fix-bug -m "Also make sure to run the linter before committing"

# auto-generated by Claude, remove when a human has sanctioned this
# 5. Connect to the agent to review its work interactively
mng conn fix-bug

# auto-generated by Claude, remove when a human has sanctioned this
# 6. When done, stop and clean up
mng stop fix-bug
mng rm fix-bug --force --remove-created-branch

# auto-generated by Claude, remove when a human has sanctioned this
# Recipe: quick iteration loop -- create, message, check, destroy
mng create scratch --no-connect --message "Write a Python script that downloads all images from a URL"
mng transcript scratch --role assistant --tail 1
mng rm scratch --force

##############################################################################
# PROJECTS
#   Agents are automatically associated with a project (the git repo you
#   run mng from). Use projects to organize agents and filter your list.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# agents inherit the project from the directory where you run mng create.
# the project is typically the name of the git repo.

# auto-generated by Claude, remove when a human has sanctioned this
# list agents for the current project only
mng ls --project my-project

# auto-generated by Claude, remove when a human has sanctioned this
# create an agent explicitly tagged with a different project
mng create my-task --project other-project

# auto-generated by Claude, remove when a human has sanctioned this
# filter agents by project using CEL expressions
mng ls --include 'project == "my-project"'

# auto-generated by Claude, remove when a human has sanctioned this
# see which projects have agents by looking at the project field
mng ls --fields "name,project,state"

##############################################################################
# MULTI-AGENT WORKFLOWS
#   Run multiple agents in parallel on different tasks, coordinate their
#   work, and bring everything together.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# launch multiple agents in parallel, each working on a different task
mng create agent-auth --no-connect --provider modal --message "Refactor the auth module to use JWT tokens"
mng create agent-tests --no-connect --provider modal --message "Add integration tests for the API endpoints"
mng create agent-docs --no-connect --provider modal --message "Update the API documentation to match the new endpoints"

# auto-generated by Claude, remove when a human has sanctioned this
# check on all of them at once
mng ls --running

# auto-generated by Claude, remove when a human has sanctioned this
# send a coordination message to all agents
mng msg -a -m "Reminder: commit and push your changes when done"

# auto-generated by Claude, remove when a human has sanctioned this
# check each agent's progress
mng transcript agent-auth --tail 2
mng transcript agent-tests --tail 2
mng transcript agent-docs --tail 2

# auto-generated by Claude, remove when a human has sanctioned this
# run git status on all agents to see what they've changed
mng exec -a -- git diff --stat

# auto-generated by Claude, remove when a human has sanctioned this
# when all are done, clean up
mng stop -a
mng rm -a --force --remove-created-branch

##############################################################################
# WORKING WITH GIT
#   Push and pull git commits (not just files) between your machine and
#   agents. Branch management, merge strategies, and worktree support.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# mng automatically creates a new branch for each agent (by default mng/{agent_name})
# this was covered in detail in the CREATING AGENTS section above

# auto-generated by Claude, remove when a human has sanctioned this
# check what branch an agent is on
mng exec my-task -- git branch --show-current

# auto-generated by Claude, remove when a human has sanctioned this
# check if the agent has uncommitted changes
mng exec my-task -- git status --short

# auto-generated by Claude, remove when a human has sanctioned this
# see the agent's recent commits
mng exec my-task -- git log --oneline -5

# auto-generated by Claude, remove when a human has sanctioned this
# have the agent commit its work
mng msg my-task -m "Please commit all your changes with a descriptive message"

# auto-generated by Claude, remove when a human has sanctioned this
# check all agents' git status at once
mng exec -a -- git status --short

# auto-generated by Claude, remove when a human has sanctioned this
# when destroying, clean up the branch that was created
mng rm my-task --force --remove-created-branch

##############################################################################
# LABELS AND FILTERING
#   Tag agents with labels and use CEL filter expressions to target
#   specific agents across list, destroy, cleanup, and other commands.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# create agents with labels for organization
mng create my-task --label team=backend --label priority=high

# auto-generated by Claude, remove when a human has sanctioned this
# list agents filtered by label using CEL expressions
mng ls --include 'labels.team == "backend"'
mng ls --include 'labels.priority == "high"'

# auto-generated by Claude, remove when a human has sanctioned this
# combine multiple filters (AND logic -- both must match)
mng ls --include 'labels.team == "backend"' --include 'state == "RUNNING"'

# auto-generated by Claude, remove when a human has sanctioned this
# exclude agents matching a filter
mng ls --exclude 'labels.team == "frontend"'

# auto-generated by Claude, remove when a human has sanctioned this
# use filters with other commands: message only backend agents
mng msg --include 'labels.team == "backend"' -m "Please run the backend test suite"

# auto-generated by Claude, remove when a human has sanctioned this
# use filters with exec: check disk usage on remote agents only
mng exec --include 'host.provider == "modal"' -- df -h /workspace

# auto-generated by Claude, remove when a human has sanctioned this
# use filters with destroy: clean up all stopped agents for a team
mng rm --include 'labels.team == "backend"' --include 'state == "STOPPED"' --force --dry-run

##############################################################################
# CREATE TEMPLATES
#   Define reusable presets that bundle common options (provider, build
#   args, permissions, environment, etc.) into a single template name.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# templates are defined in your config (user, project, or local scope).
# here's how to set one up using the config command:
mng config edit --scope project
# in the editor, add something like:
#   [create_templates.modal-gpu]
#   provider = "modal"
#   build_args = ["gpu=A10G", "cpu=4", "memory=16"]
#   idle_timeout = "120"
#   agent_args = ["--dangerously-skip-permissions"]

# auto-generated by Claude, remove when a human has sanctioned this
# then use the template when creating agents:
mng create my-task --template modal-gpu

# auto-generated by Claude, remove when a human has sanctioned this
# short form
mng create my-task -t modal-gpu

# auto-generated by Claude, remove when a human has sanctioned this
# stack multiple templates (later templates override earlier ones)
mng create my-task -t modal-gpu -t with-tests

# auto-generated by Claude, remove when a human has sanctioned this
# templates are covered in more detail in the CREATING AGENTS section above

##############################################################################
# CUSTOM AGENT TYPES
#   Define your own agent types in config, or use any command in your PATH
#   as an agent. Wrap existing tools with custom defaults and permissions.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# mng supports multiple agent types out of the box (claude, codex, etc.)
# you can also run any command as an "agent" using --command:
mng create my-server --command python -- -m http.server 8080

# auto-generated by Claude, remove when a human has sanctioned this
# run a custom script as an agent
mng create my-task --command /path/to/my-tool -- --some-flag

# auto-generated by Claude, remove when a human has sanctioned this
# agent types are provided by plugins -- see MANAGING PLUGINS above
# to see which agent types are available:
mng plugin list --active

# auto-generated by Claude, remove when a human has sanctioned this
# you can specify the agent type as the second positional argument to create:
mng create my-task claude
mng create my-task codex

##############################################################################
# ENVIRONMENT VARIABLES
#   Pass environment variables to agents during creation, control mng
#   behavior via env vars, and understand the variables mng sets for you.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# set environment variables for the agent at creation time
mng create my-task --env DEBUG=true --env LOG_LEVEL=verbose

# auto-generated by Claude, remove when a human has sanctioned this
# load environment variables from a file (recommended for sensitive values)
mng create my-task --env-file .env.agent

# auto-generated by Claude, remove when a human has sanctioned this
# forward an environment variable from your current shell
export ANTHROPIC_API_KEY=sk-ant-...
mng create my-task --pass-env ANTHROPIC_API_KEY

# auto-generated by Claude, remove when a human has sanctioned this
# set host-level environment variables (for the host OS, not the agent process)
mng create my-task --provider modal --pass-host-env MODAL_TOKEN_ID --pass-host-env MODAL_TOKEN_SECRET

# auto-generated by Claude, remove when a human has sanctioned this
# control mng itself via environment variables (all config options can be set this way)
export MNG_HEADLESS=true
export MNG_COMMANDS__CREATE__PROVIDER=modal
mng create my-task

##############################################################################
# RUNNING AGENTS ON MODAL
#   Launch agents in Modal sandboxes for full isolation, GPU access, and
#   cloud-based execution. Custom images, secrets, volumes, and networking.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# basic Modal agent (covered in more detail in the CREATING AGENTS REMOTELY section above)
mng create my-task --provider modal

# auto-generated by Claude, remove when a human has sanctioned this
# specify CPU, memory, and GPU resources
mng create my-task --provider modal -b cpu=4 -b memory=16 -b gpu=A10G

# auto-generated by Claude, remove when a human has sanctioned this
# use a custom Docker image as the base
mng create my-task --provider modal -b image=python:3.12

# auto-generated by Claude, remove when a human has sanctioned this
# use a custom Dockerfile
mng create my-task --provider modal -b file=./Dockerfile.agent

# auto-generated by Claude, remove when a human has sanctioned this
# mount a persistent volume for data that survives host destruction
mng create my-task --provider modal -b volume=my-data:/data

# auto-generated by Claude, remove when a human has sanctioned this
# set an idle timeout to avoid runaway costs
mng create my-task --provider modal --idle-timeout 120

# auto-generated by Claude, remove when a human has sanctioned this
# create a snapshot for checkpointing (useful before risky changes)
mng snap create my-task --name "checkpoint-1"

# auto-generated by Claude, remove when a human has sanctioned this
# list all Modal agents
mng ls --provider modal

# auto-generated by Claude, remove when a human has sanctioned this
# garbage collect unused Modal resources
mng gc --provider modal --dry-run

##############################################################################
# RUNNING AGENTS IN DOCKER
#   Run agents in Docker containers for local isolation without cloud
#   costs. Good for untrusted code or reproducible environments.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# run an agent in a local Docker container
mng create my-task --provider docker

# auto-generated by Claude, remove when a human has sanctioned this
# pass Docker-specific start args (eg, GPU access)
mng create my-task --provider docker -s "--gpus all"

# auto-generated by Claude, remove when a human has sanctioned this
# use a custom Dockerfile for the container image
mng create my-task --provider docker -b file=./Dockerfile.dev

# auto-generated by Claude, remove when a human has sanctioned this
# set resource limits via build args
mng create my-task --provider docker -b cpu=2 -b memory=8

# auto-generated by Claude, remove when a human has sanctioned this
# list Docker agents
mng ls --provider docker

# auto-generated by Claude, remove when a human has sanctioned this
# garbage collect unused Docker resources
mng gc --provider docker --dry-run

##############################################################################
# IDLE DETECTION AND TIMEOUTS
#   Automatically pause or stop agents when they go idle to save resources.
#   Configure what counts as "activity" and how long to wait.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# set an idle timeout (in seconds) -- the agent's host will stop after this much inactivity
mng create my-task --provider modal --idle-timeout 60

# auto-generated by Claude, remove when a human has sanctioned this
# control what counts as "activity" with --idle-mode:
#   "agent" (default) -- idle when the agent process is idle
#   "ssh" -- idle when no SSH sessions are connected
#   "run" -- idle when the main process exits (useful for non-agent commands)
mng create my-task --provider modal --idle-mode ssh --idle-timeout 300

# auto-generated by Claude, remove when a human has sanctioned this
# for long-running scripts, "run" mode stops the host when the script finishes
mng create my-task --provider modal --command python --idle-mode run --idle-timeout 60 -- long_job.py

# auto-generated by Claude, remove when a human has sanctioned this
# see the idle_detection.md file for more details on idle detection strategies

##############################################################################
# MULTIPLE AGENTS ON ONE HOST
#   Run several agents on the same host to share resources and reduce
#   costs. Agents share the host filesystem and network.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# create a first agent on a named host
mng create agent-1@shared-host.modal --provider modal --new-host

# auto-generated by Claude, remove when a human has sanctioned this
# create additional agents on the same host using the address syntax
mng create agent-2@shared-host.modal

# auto-generated by Claude, remove when a human has sanctioned this
# all agents on the same host share the filesystem and network,
# so they can collaborate on the same codebase

# auto-generated by Claude, remove when a human has sanctioned this
# list agents to see which ones share a host
mng ls --fields "name,state,host.name"

# auto-generated by Claude, remove when a human has sanctioned this
# stop one agent without affecting the others
mng stop agent-1

# auto-generated by Claude, remove when a human has sanctioned this
# the host stays running as long as at least one agent is active.
# if you need the host to stay up even with no agents, use --no-start-on-boot
# and manage the host lifecycle manually.

##############################################################################
# RUNNING NON-AGENT PROCESSES
#   mng is useful for more than just AI agents! Run any long-lived process (like servers, data pipelines, etc.)
#   with mng to get the same benefits of easy management, logging, and remote execution.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# run a Python script as a managed process
mng create my-server --command python -- -m http.server 8080

# auto-generated by Claude, remove when a human has sanctioned this
# run a long-running data pipeline
mng create etl-job --command python --idle-mode run --idle-timeout 60 -- etl_pipeline.py

# auto-generated by Claude, remove when a human has sanctioned this
# run a dev server with extra tmux windows for logs
mng create dev-env --command "npm run dev" -w logs="tail -f /var/log/app.log"

# auto-generated by Claude, remove when a human has sanctioned this
# use --idle-mode run so the host stops when the process finishes
mng create batch-job --provider modal --command bash --idle-mode run --idle-timeout 30 -- -c "python train.py && python evaluate.py"

# auto-generated by Claude, remove when a human has sanctioned this
# snapshot the host after the process completes (connect later to inspect results)
mng snap create batch-job --name "after-training"

# auto-generated by Claude, remove when a human has sanctioned this
# connect to inspect the results
mng conn batch-job --start

##############################################################################
# SCRIPTING AND AUTOMATION
#   Use mng in shell scripts, CI pipelines, and cron jobs. JSON output,
#   headless mode, idempotent creation, and programmatic control.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# run in headless mode (no interactive prompts)
mng create my-task --headless --no-connect --message "Do the thing"

# auto-generated by Claude, remove when a human has sanctioned this
# or set headless globally
mng config set headless true

# auto-generated by Claude, remove when a human has sanctioned this
# idempotent creation: reuse an existing agent if it already exists
mng create worker --reuse --provider modal --no-connect --message "Process the queue"

# auto-generated by Claude, remove when a human has sanctioned this
# get JSON output for parsing in scripts
AGENT_INFO=$(mng ls --format json)

# auto-generated by Claude, remove when a human has sanctioned this
# use JSONL for streaming results into other tools
mng ls --stream --format jsonl | while read -r line; do
  echo "$line" | python -c "import sys, json; d=json.load(sys.stdin); print(d.get('name', 'unknown'))"
done

# auto-generated by Claude, remove when a human has sanctioned this
# run a command on an agent and capture the output
RESULT=$(mng exec my-task -- cat /workspace/output.txt)

# auto-generated by Claude, remove when a human has sanctioned this
# check if an agent is running (useful in CI scripts)
mng ls --running --format json | python -c "
import sys, json
agents = json.load(sys.stdin)
names = [a['name'] for a in agents]
sys.exit(0 if 'my-task' in names else 1)
"

##############################################################################
# OUTPUT FORMATS AND MACHINE-READABLE OUTPUT
#   Switch between human-readable, JSON, and JSONL output. Use --format
#   with templates, pipe output to jq, and build tooling on top of mng.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# default output is human-readable
mng ls

# auto-generated by Claude, remove when a human has sanctioned this
# JSON output (full array, good for programmatic use)
mng ls --format json

# auto-generated by Claude, remove when a human has sanctioned this
# JSONL output (one object per line, good for streaming/piping)
mng ls --format jsonl

# auto-generated by Claude, remove when a human has sanctioned this
# stream JSONL results as they arrive (don't wait for all results)
mng ls --stream --format jsonl

# auto-generated by Claude, remove when a human has sanctioned this
# JSON works with most commands
mng snapshot list --format json
mng plugin list --format json
mng config list --format json

# auto-generated by Claude, remove when a human has sanctioned this
# combine with jq for powerful filtering and transformation
mng ls --format json | jq '.[] | select(.state == "RUNNING") | .name'

# auto-generated by Claude, remove when a human has sanctioned this
# use custom format templates
mng ls --format '{agent.name} ({agent.state})'

##############################################################################
# UPLOADING FILES AND RUNNING SETUP COMMANDS
#   Upload files, append to configs, create directories, and run setup
#   commands on agent hosts during creation or via re-provisioning.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# upload a file to the agent's host during creation
mng create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config

# auto-generated by Claude, remove when a human has sanctioned this
# run a setup command during host provisioning
mng create my-task --provider modal --user-command "pip install numpy pandas"

# auto-generated by Claude, remove when a human has sanctioned this
# run a command as root during provisioning
mng create my-task --provider modal --sudo-command "apt-get update && apt-get install -y vim"

# auto-generated by Claude, remove when a human has sanctioned this
# append content to a file on the host
mng create my-task --provider modal --append-to-file /root/.bashrc="export PATH=/opt/bin:\$PATH"

# auto-generated by Claude, remove when a human has sanctioned this
# combine multiple setup steps
mng create my-task --provider modal \
  --upload-file ./requirements.txt:/workspace/requirements.txt \
  --sudo-command "apt-get update && apt-get install -y build-essential" \
  --user-command "pip install -r /workspace/requirements.txt"

##############################################################################
# ADVANCED WORKFLOWS
#   Complex multi-agent setups, custom scripts, and integrations with other
#   tools and platforms. Examples of building agent orchestration, custom dashboards, and more.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# fan-out pattern: create many agents from a list of tasks
for task in "fix-auth" "add-logging" "update-deps" "write-docs"; do
  mng create "$task" --provider modal --no-connect --message "Work on: $task"
done

# auto-generated by Claude, remove when a human has sanctioned this
# monitor all agents in a watch loop
mng ls --watch 5 --running

# auto-generated by Claude, remove when a human has sanctioned this
# collect results from all agents
for agent in "fix-auth" "add-logging" "update-deps" "write-docs"; do
  echo "=== $agent ==="
  mng exec "$agent" -- git log --oneline -3
done

# auto-generated by Claude, remove when a human has sanctioned this
# snapshot all agents before a risky operation
mng snap create -a --name "pre-merge-checkpoint"

# auto-generated by Claude, remove when a human has sanctioned this
# batch cleanup: stop all agents, then destroy them
mng stop -a
mng rm -a --force --remove-created-branch
mng gc

##############################################################################
# TIPS AND TRICKS
#   Power-user shortcuts, lesser-known features, and workflow patterns
#   that make working with mng faster and more pleasant.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# use short forms for common commands to save typing
# mng c = mng create, mng ls = mng list, mng s = mng stop, mng rm = mng destroy
# mng conn = mng connect, mng msg = mng message, mng x = mng exec

# auto-generated by Claude, remove when a human has sanctioned this
# use --reuse to make create idempotent (great for muscle memory / re-running scripts)
mng create my-task --reuse --provider modal

# auto-generated by Claude, remove when a human has sanctioned this
# use --dry-run on destructive commands to preview what will happen
mng rm -a --dry-run
mng stop -a --dry-run
mng gc --dry-run

# auto-generated by Claude, remove when a human has sanctioned this
# use --watch with list to keep a live dashboard in a terminal
mng ls --watch 3

# auto-generated by Claude, remove when a human has sanctioned this
# use exec to quickly inspect an agent's environment
mng x my-task -- env | sort
mng x my-task -- df -h
mng x my-task -- free -h

# auto-generated by Claude, remove when a human has sanctioned this
# check the transcript to see what an agent has been up to
mng transcript my-task --tail 5

# auto-generated by Claude, remove when a human has sanctioned this
# use events to debug agent lifecycle issues
mng events my-task --tail 10

##############################################################################
# TROUBLESHOOTING
#   Common problems and how to fix them. Debugging with logs, verbose
#   output, and exec. What to do when agents crash or hosts won't start.
##############################################################################


# auto-generated by Claude, remove when a human has sanctioned this
# check if the agent exists and its current state
mng ls --fields "name,state,host.provider,host.name"

# auto-generated by Claude, remove when a human has sanctioned this
# view recent events to understand what happened
mng events my-task --tail 20

# auto-generated by Claude, remove when a human has sanctioned this
# follow events in real time while reproducing an issue
mng events my-task --follow

# auto-generated by Claude, remove when a human has sanctioned this
# check the agent's transcript for error messages
mng transcript my-task --tail 10

# auto-generated by Claude, remove when a human has sanctioned this
# run commands on the host to diagnose issues
mng x my-task -- cat /var/log/syslog | tail -20
mng x my-task -- ps aux
mng x my-task -- df -h

# auto-generated by Claude, remove when a human has sanctioned this
# if an agent is stuck, try stopping and restarting it
mng stop my-task
mng start my-task --connect

# auto-generated by Claude, remove when a human has sanctioned this
# if a host is in a bad state, destroy and recreate
mng rm my-task --force
mng create my-task --provider modal

# auto-generated by Claude, remove when a human has sanctioned this
# garbage collect to clean up any orphaned resources
mng gc --dry-run
mng gc
