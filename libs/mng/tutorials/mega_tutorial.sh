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
mng create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config --extra-provision-command "pip install foo"
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

# list all agents
mng list

# short form
mng ls

# show only running agents
mng list --running

# show only stopped agents (not running, still exists and can be restarted)
mng list --stopped

# show only archived agents (stopped, cannot necessarily be restarted, but data can be inspected)
mng list --archived

# show only active agents (anything not archived/destroyed/crashed/failed)
mng list --active

# show only agents running locally
mng list --local

# show only agents running remotely
mng list --remote

# filter by provider
mng list --provider modal

# filter by project
mng list --project my-project

# filter by agent label
mng list --label TEAM=backend

# filter by host label
mng list --host-label ENV=staging

# choose which fields to display and sort order
mng list --fields "name,state,host.provider,created_at" --sort "-created_at"
# see mng list --help for a complete list of fields you can reference

# limit the number of results
mng list --limit 10

# watch mode: refresh the list every 5 seconds
watch -n5 mng list

# output all objects as one bit json array when complete  (useful for scripting)
mng list --format json

# output each entry as a JSON object (useful for scripting)
mng list --format jsonl

# continually stream results as JSONL (useful for piping to jq to turn this data into an event stream)
# will get new events as new hosts are created/destroyed, come online and offline, etc.
# see the `DiscoveryEvent` type for a complete list of the event types that will be returned in this stream
mng list --stream --format jsonl

# you can pass the ids of agents and/or hosts to only list details for specific ids:
mng list --format "{id}" | head -n 2 | mng list --stdin

##############################################################################
# CONNECTING TO AGENTS
#   If you've disconnected from an agent (or created one with --no-connect),
#   you can reconnect to it at any time.
##############################################################################

# connect to a running agent by name
mng connect my-task

# short form
mng conn my-task

# sometimes names can be ambiguous (e.g. if you made two agents with the same name on different hosts), so you can always
# be really specific by using the agent id instead of the name:
mng connect agent-fa29307a16734899aa77b0f0563c8c99

# or you can use the explicit host and agent:
mng conn my-task@my-host

# or if you're really unlucky and have multiple *hosts* with the same name (across different providers),
# you can use the explicit host, agent and provider:
mng conn my-task@my-host.modal

# the default behavior is to start the agent if it's stopped (you can be explicit about that too):
mng connect my-task --start

# or you can disable auto-starting (fails if agent is stopped)
mng connect my-task --no-start

##############################################################################
# SENDING MESSAGES TO AGENTS
#   You can send messages to running agents without connecting to them.
#   This is useful for giving agents new instructions while they work.
##############################################################################

# send a message to a specific agent
mng message my-task -m "Please also add unit tests for the new function"

# short form
mng msg my-task -m "Check the CI results and fix any failures"

# send the same message to multiple agents by name
mng msg agent-1 agent-2 agent-3 -m "Wrap up and commit your changes"

# send a message to all agents
mng msg -a -m "Stop what you are doing and commit your current progress"

# send a message to agents matching a filter
mng list --include 'host.provider == "modal"' --ids | mng msg - -m "Almost out of budget, please finish up"

# control error handling when messaging multiple agents
# your choices are:
#   "continue", which means try all agents once, or
#   "abort", which means stop if any agent fails to receive the message
# note that "abort" is kind of dangerous--you could easily have agents left in a strange state
# thus the default is "continue"
mng msg -a -m "Status update please" --on-error continue

##############################################################################
# EXECUTING COMMANDS ON AGENTS
#   Run shell commands on an agent's host without connecting interactively.
#   Useful for scripting, checking status, or running one-off operations.
##############################################################################

# run a command on a specific agent's host
mng exec my-task "ls -la /workspace"
# note that the command must be quoted--it's the last argument passed to "mng exec"
# the quoting is required because e.g. this may be sent over SSH

# short form
mng x my-task "git status"

# run a command on all agents
mng exec -a "whoami"

# run a command as a specific user as you normally would on that host (ex: sudo -u other-user)
mng exec my-task "sudo -u other-user apt-get update"

# run a command in a specific working directory
mng exec my-task --cwd /tmp "pwd"
# by default, commands are run in the agent's work_dir

# set a timeout (in seconds) for the command
mng exec my-task --timeout 30 "python long_script.py"

# by default, start the agent's host if it's stopped, run the command, then leave it running
# but you can be explicit about that behavior:
mng exec my-task --start "cat /etc/os-release"

# and you can disable auto-starting as well (fails if agent is stopped):
mng exec my-task --no-start "cat /etc/os-release"

# control error handling when running on multiple agents
mng exec -a --on-error continue "git log --oneline -5"
# the choices for --on-error are the same as for messaging: "continue" (try all agents) and "abort" (stop if any agent fails)

# FIXME: sure, these might be experimental, but they could at least use some tests! I think they work in theory...
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


# start a stopped agent. Is idempotent, so is safe to call even if already running.
mng start my-task

# start a stopped agent and immediately connect to it
mng start my-task --connect

# start multiple agents at once
mng start agent-1 agent-2 agent-3

# start all stopped agents by simply passing their ids from "mng list" and reading the ids from stdin (that's what the "-" means)
mng list --ids | mng start -

# dry-run to see what would happen without actually starting anything
mng list --ids | mng start - --dry-run

# stop a running agent
mng stop my-task

# stop and archive the agent (creates a snapshot before stopping).
mng stop my-task --archive

# you can also archive an agent via the "archive" command, which is basically just a shortcut for "stop --archive"
mng archive my-task

# stop all running agents
mng list --ids | mng stop -

# dry-run to see what would be stopped
mng list --ids | mng stop - --dry-run

# stop has a special variant for finding an agent by its tmux session name:
mng stop --session my-session-name
# this is used primarily to implement the hotkey for exiting from tmux (ex: ctrl-t)

##############################################################################
# DESTROYING AGENTS
#   When you're done with an agent, destroy it to clean up all of its
#   resources (host, snapshots, volumes, etc.).
##############################################################################

# destroy a specific agent
mng destroy my-task

# short form
mng rm my-task

# destroy without confirmation prompt
mng destroy my-task --force

# destroy and also remove the git branch that was created for the agent
# this is not the default because it can be annoying to lose the changes, so we default to the safe option
mng destroy my-task --force --remove-created-branch

# destroy multiple agents at once
mng destroy agent-1 agent-2 agent-3 --force

# destroy all agents (be careful!)
mng list --ids | mng destroy - --force

# dry-run to see what would be destroyed without actually doing it
mng list --ids | mng destroy - --dry-run

# destroy and run garbage collection afterward (this is the default)
mng destroy my-task --force --gc

# by default, gc (garbage collection) runs after destroying any agent
# you can disable this if you want:
mng destroy --no-gc
# however, note that it is generally a good idea to ensure that "mng gc" is run periodically,
# otherwise resources (ex: worktrees, hosts, containers, volumes, etc) will accumulate over time

# destroy has a special variant for finding an agent by its tmux session name:
mng destroy --session my-session-name
# this is used primarily to implement the hotkey for exiting from tmux (ex: ctrl-q)

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


# create a snapshot of an agent's host
mng snapshot create my-task

# short form
mng snap create my-task

# create a snapshot with a descriptive name
mng snapshot create my-task --name "before-refactor"

# snapshot all agents' hosts
mng list --ids | mng snapshot create -

# auto-generated by Claude, remove when a human has sanctioned this
# dry-run to see what would be snapshotted
mng snapshot create my-task --dry-run

# list all snapshots
mng snapshot list

# list snapshots for a specific agent's host
mng snapshot list my-task

# limit the number of snapshots shown
mng snapshot list my-task --limit 5

# destroy a specific snapshot
mng snapshot destroy --snapshot snap-123abc

# destroy all snapshots for an agent's host
mng snapshot destroy my-task --all-snapshots --force

# dry-run to see what would be destroyed
mng snapshot destroy my-task --all-snapshots --dry-run

# TODO: I think it'd be worth going through the provision ones sooner rather than later...
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


# garbage collect all unused resources
mng gc

# if you want to see what would be cleaned before actually running garbage collection
mng gc --dry-run

# garbage collect for a specific provider only (repeatable if you want multiple providers)
mng gc --provider modal

# if you wanted, you could disable automatic garbage collection on destroy by setting the appropriate setting:
mng config set commands.destroy.gc false
# then make sure you constantly run gc in the background (this runs it once every 60 seconds)
watch -n60 mng gc
# this would have the effect of making your calls to "mgn destroy" somewhat faster, at the cost of needing to have this background process running

##############################################################################
# VIEWING EVENTS AND LOGS
#   View event stream and log files for agents and hosts. Useful for debugging and
#   monitoring what your agents are up to.
##############################################################################

# view all events for an agent
mng events my-task
# all events are json objects that are guaranteed to have at least the following fields: "event_id", "timestamp", "source" and "type"
# events are printed as JSONL (one JSON object per line), so you can easily pipe them to jq for filtering and formatting, or to other tools for monitoring and alerting

# follow events in real time (like tail -f). Extremely useful for scripting.
mng events my-task --follow

# restrict the event stream to a specific type of event (source)
# in this case we're looking at the "claude/common_transcript" events for a claude agent,
# which shows the conversation messages in and out of the agent in a unified format
mng events my-task --follow claude/common_transcript

# show only the last 20 events
mng events my-task --tail 20

# show only the first 10 events
mng events my-task --head 10

# filter events using a CEL expression
mng events my-task --filter 'type == "user_message"'

# view the transcript of an agent's conversation
mng transcript my-task

# view only assistant messages
mng transcript my-task --role assistant

# view the last 5 messages
mng transcript my-task --tail 5

# output transcript as JSONL for programmatic use
mng transcript my-task --format jsonl

##############################################################################
# MANAGING PLUGINS
#   List, enable, and disable plugins that extend mng with new agent types,
#   provider backends, and CLI commands.
##############################################################################


# list all available plugins
mng plugin list

# list only active plugins
mng plugin list --active

# add a plugin by name (from the registry)
mng plugin add my-plugin

# add a plugin from a local path
mng plugin add --path /path/to/my-plugin

# add a plugin from a git repository
mng plugin add --git https://github.com/user/mng-plugin.git

# remove a plugin
mng plugin remove my-plugin

# enable a plugin at the project scope
mng plugin enable my-plugin --scope project

# disable a plugin at the user scope
mng plugin disable my-plugin --scope user

# list plugins with specific fields
mng plugin list --fields "name,version,active"


##############################################################################
# CONFIGURATION
#   Customize mng's behavior via configuration files. Set defaults for
#   commands, define create templates, and configure providers.
##############################################################################


# list all configuration values
mng config list

# list configuration at a specific scope (user, project, or local)
mng config list --scope user
mng config list --scope project
mng config list --scope local

# get a specific config value
mng config get commands.create.provider

# set a config value (at the default scope)
mng config set commands.create.provider modal

# set a config value at a specific scope
mng config set headless true --scope user

# unset a config value
mng config unset commands.create.provider

# open the config file in your editor
mng config edit

# open a specific scope's config file
mng config edit --scope project

# show the path to the config file
mng config path

# show the path to a specific scope's config file
mng config path --scope user

##############################################################################
# COMMON TASKS
#   Quick recipes for the things you'll do most often: launching an agent
#   on a task, checking on it, grabbing its work, and cleaning up after.
##############################################################################


# Recipe: launch an agent on a task, check on it later, and clean up
# 1. Create an agent with a task, don't connect (let it work in the background)
mng create fix-bug --provider modal --no-connect --message "Fix the failing test in test_auth.py and make a PR"
# 2. Check what agents are running
mng list --running
# 3. Check the agent's conversation to see its progress
mng transcript fix-bug --tail 3
# 4. Send a follow-up message if needed
mng msg fix-bug -m "Also make sure to run the linter before committing"
# 5. Connect to the agent to review its work interactively
mng conn fix-bug
# 6. merge the resulting branch
git merge mng/fix-bug
# 7. When done, stop and clean up
mng destroy fix-bug --f --remove-created-branch

# TODO: LOTS more examples to add here!

##############################################################################
# PROJECTS
#   Agents are automatically associated with a project (the git repo you
#   run mng from). Use projects to organize agents and filter your list.
##############################################################################


# agents inherit the project from the directory where you run mng create.
# the project is typically the name of the git repo.
# list agents for the current project only
mng list --project my-project

# create an agent explicitly tagged with a different project
mng create my-task --project other-project

# filter agents by project using CEL expressions
mng list --include 'project == "my-project"'

# see which projects have agents by looking at the project field
mng list --fields "name,project,state"

##############################################################################
# MULTI-AGENT WORKFLOWS
#   Run multiple agents in parallel on different tasks, coordinate their
#   work, and bring everything together.
##############################################################################

# launch multiple agents in parallel, each working on a different task
mng create agent-auth --no-connect --provider modal --message "Refactor the auth module to use JWT tokens"
mng create agent-tests --no-connect --provider modal --message "Add integration tests for the API endpoints"
mng create agent-docs --no-connect --provider modal --message "Update the API documentation to match the new endpoints"
# check on all of them at once
mng list --running
# wait for them to finish
mng wait agent-auth && mng wait agent-tests && mng wait agent-docs
# run git status on all agents to see what they've changed
mng list --ids | mng exec - "git diff --stat"
# send a coordination message to all agents
mng msg -a -m "Reminder: commit and push your changes when done"
# merge all of the changes
git merge mng/agent-auth
git merge mng/agent-tests
git merge mng/agent-docs
# when all are done, clean up
mng destroy --force --remove-created-branch agent-auth agent-tests agent-docs

# TODO: LOTS more examples to add here! Including
#  - running multiple claudes via the tmux windows
#  - running different types of agents together in the same session (ex: as a reviewer)
#  - sampling by launching the same task multiple times
#  - comparing outputs of different models and harnesses on the same task


##############################################################################
# WORKING WITH GIT
#   Push and pull git commits (not just files) between your machine and
#   agents. Branch management, merge strategies, and worktree support.
##############################################################################


# by default, mng automatically creates a new branch for each agent (default: mng/{agent_name})
# you can specify the base branch, or disable branch creation if you want to work on an existing branch instead
# all of that was covered in detail in the CREATING AGENTS section above

# check what branch an agent is on (it may have shifted if the agent checked out a new branch)
mng exec my-task "git branch --show-current"

# TODO: this field name isn't right, go fix (but that info is there somewhere in mng list)
# you can see the original branch as part of the details in "mng list" as well (field name: "git.original_branch")
mng list --fields "name,state,git.original_branch"

# check if the agent has uncommitted changes
mng exec my-task "git status --short"

# see the agent's recent commits
mng exec my-task "git log --oneline -5"

# ask the agent commit its work
mng msg my-task -m "Please commit all your changes with a descriptive message"

# or forcibly commit all of it yourself
mng exec my-task 'git add . && git commit -am "Please commit all your changes with a descriptive message"'

# check all agents' git status at once
mng list --ids | mng exec - "git status --short"

# merge the agent's work like normal if the agent is local:
git merge mng/my-task

# and if remote, force the agent to push, then fetch and merge:
mng exec my-task "git push origin mng/my-task"
git fetch --all && git merge mng/my-task
# in general, you should probably just tell your agents to automatically push / create PRs when it makes sense

# TODO: give an example of how to use a stop hook to automatically push for the agent

# TODO: add some more example with mng pull for how to merge work back in

# when destroying, clean up the branch that was originally created when the agent was created
mng destroy my-task --force --remove-created-branch

##############################################################################
# LABELS AND FILTERING
#   Tag agents with labels and either use CEL filter expressions to target
#   specific agents, or just use jq. Filter agentse for destroy, cleanup, and other commands
#   by piping in the names or ids from a call to mng list
##############################################################################


# create agents with labels for organization
mng create my-task --label team=backend --label priority=high

# list agents filtered by label using CEL expressions
mng list --include 'labels.priority == "high"'

# combine multiple filters (AND logic for --include, all must match)
mng list --include 'labels.team == "backend"' --include 'state == "RUNNING"'

# exclude agents matching a filter
mng list --exclude 'labels.team == "frontend"'

# combine multiple exclusion filters (OR logic for --exclude, any can match)
mng list --exclude 'labels.team == "frontend"' --exclude 'labels.team == "devops"'

# you can also just do combined filters directly in the CEL expression:
mng list --include 'labels.team == "backend" && state == "RUNNING"'

# use filters with other commands: message only backend agents by passing "-" to have the list of matching agents piped in via stdin
mng list --include 'labels.team == "backend"' --ids | mng message - -m "Please run the backend test suite"

# use filters with exec: check disk usage on remote agents only
mng list --include 'host.provider == "modal"' --ids | mng exec - "df -h /workspace"

# use filters with destroy: clean up all stopped agents for a team
mng list --include 'labels.team == "backend"' --include 'state == "STOPPED"' --ids | mng destroy - --force --dry-run

# you can also just list agents by filtering using jq:
mng list --format json | jq '.[] | select(.labels.priority == "high")'

# or even stream the filters with jq by using jsonl:
mng list --format jsonl | jq --unbuffered 'select(.labels.priority == "high")'


##############################################################################
# CREATE TEMPLATES
#   Define reusable presets that bundle common options (provider, build
#   args, permissions, environment, etc.) into a single template name.
##############################################################################

# templates are defined in your config (user, project, or local scope).
# here's how to set one up using the config command:
mng config edit --scope project
# in the editor, add something like:
#   [create_templates.modal-big]
#   provider = "modal"
#   build_args = ["cpu=4", "memory=16"]
#   idle_timeout = "120"
#   agent_args = ["--dangerously-skip-permissions"]
# then use the template when creating agents:
mng create my-task --template modal-big

# short form
mng create my-task -t modal-big

# stack multiple templates (later templates override earlier ones)
mng create my-task -template modal-big -template with-tests

##############################################################################
# CUSTOM AGENT TYPES
#   Define your own agent types in config, or use any command in your PATH
#   as an agent. Wrap existing tools with custom defaults and permissions.
##############################################################################

# mng supports multiple agent types out of the box (claude, codex, etc.)
# you can also run any command as an "agent" using --command:
mng create my-server --command python -- -m http.server 8080

# run a custom script as an agent
mng create my-task --command /path/to/my-tool -- --some-flag

# agent types are provided by plugins -- see MANAGING PLUGINS above
# to see which agent types are available:
mng plugin list --active

# you can specify the agent type as the second positional argument to create:
mng create my-task codex

# or by specifying it explicitly
mng create my-task --type codex

# you can also create your own custom agent types by defining them in a config:
# here's how to set one up using the config command:
mng config edit --scope project
# in the editor, add something like:
#   [agent_types.yolo]
#   parent_type = "claude"
#   cli_args = "--dangerously-skip-permissions"
# then you can create agents of that type:
mng create my-task yolo
# you'll have to look at the agent config class for each agent type to know what config options are supported

# FIXME: make those plugins actually show those config options

##############################################################################
# ENVIRONMENT VARIABLES
#   Pass environment variables to agents during creation, control mng
#   behavior via env vars, and understand the variables mng sets for you.
##############################################################################

# set environment variables for the agent at creation time
mng create my-task --env DEBUG=true --env LOG_LEVEL=verbose

# load environment variables from a file (recommended for sensitive values, eg, secrets/api keys/tokens/etc)
mng create my-task --env-file .env.agent

# forward an environment variable from your current shell
export ANTHROPIC_API_KEY=sk-ant-...
mng create my-task --pass-env ANTHROPIC_API_KEY

# set host-level environment variables (for all agents on the host, not just that particular agent process)
mng create my-task --provider modal --pass-host-env MODAL_TOKEN_ID --pass-host-env MODAL_TOKEN_SECRET

# control mng itself via environment variables. All config options can be set this way, use double-underscore ("__")
# in order to index into the nested config structure. For example, to set the provider to "modal" for a create command:
export MNG_COMMANDS__CREATE__PROVIDER=modal
mng create my-task

##############################################################################
# RUNNING AGENTS ON MODAL
#   Launch agents in Modal sandboxes for full isolation, GPU access, and
#   cloud-based execution. Custom images, secrets, volumes, and networking.
##############################################################################

# basic Modal agent (also covered in the CREATING AGENTS REMOTELY section above)
mng create my-task --provider modal

# specify CPU, memory, and GPU resources
mng create my-task --provider modal -b cpu=4 -b memory=16 -b gpu=A10G

# use a custom Docker image as the base
mng create my-task --provider modal -b image=python:3.12

# use a custom Dockerfile
mng create my-task --provider modal -b file=./Dockerfile.agent

# mount a persistent volume for data that survives host destruction
mng create my-task --provider modal -b volume=my-data:/data

# set an idle timeout to avoid runaway costs
mng create my-task --provider modal --idle-timeout 120

# create a snapshot for checkpointing (useful before risky changes)
mng snapshot create my-task --name "checkpoint-1"

# list all Modal agents
mng list --provider modal

# destroy all Modal agents (be careful!)  Useful for cleaning up while prototyping
mng list --include 'host.provider == "modal"' --ids | mng destroy -f

##############################################################################
# RUNNING AGENTS IN DOCKER
#   Run agents in Docker containers for local isolation without cloud
#   costs. Good for untrusted code or reproducible environments.
##############################################################################

# run an agent in a local Docker container. Will default to mng's default image if you don't specify one.
mng create my-task --provider docker

# use a custom Dockerfile for the container image. One strange thing is that you probably want to pass "-b ." because
# that's just how docker works (it takes the context dir as the last arg)
mng create my-task --provider docker -b file=./Dockerfile.dev -b .

# pass Docker-specific start args (eg, GPU access) "start args" are the args to "docker run", see "docker run --help" for all of them
mng create my-task --provider docker -s "--gpus all"

# include additional volumes for data persistence and sharing
mng create my-task --provider docker -s "-v /host/data:/container/data"
# note that all docker hosts have a default volume mounted, which is used so that the host and agent information can be
# available even when a given "host" (container) is stopped

# set resource limits via start args
mng create my-task --provider docker -s cpus=2

# list Docker agents
mng list --provider docker

# destroy all docker agents (be careful!)  Useful for cleaning up while prototyping
mng list --include 'host.provider == "docker"' --ids | mng destroy -f

##############################################################################
# IDLE DETECTION AND TIMEOUTS
#   Automatically pause or stop agents when they go idle to save resources.
#   Configure what counts as "activity" and how long to wait.
##############################################################################

# set an idle timeout (in seconds) -- the agent's host will stop after this much inactivity
mng create my-task --provider modal --idle-timeout 60

# control what counts as "activity" with --idle-mode:
#   "agent" (default) -- idle when the agent process is idle
#   "ssh" -- idle when no SSH sessions are connected
#   "run" -- idle when the main process exits (useful for non-agent commands)
#   ...
# see the idle_detection.md file for more details on idle detection strategies
mng create my-task --provider modal --idle-mode ssh --idle-timeout 300

# for long-running scripts, "run" mode stops the host when the script finishes
mng create my-task --provider modal --command python --idle-mode run --idle-timeout 60 -- long_job.py

# TODO: make a few more examples here--there's lots of useful stuff you can do with this!

##############################################################################
# MULTIPLE AGENTS ON ONE HOST
#   Run several agents on the same host to share resources and reduce
#   costs. Agents share the host filesystem and network.
##############################################################################

# create a first agent on a named host
mng create agent-1@shared-host.modal --provider modal --new-host
# create additional agents on the same host using the address syntax
mng create agent-2@shared-host.modal
# all agents on the same host share the filesystem and network,
# so they can collaborate on the same codebase
# list agents to see which ones share a host
mng list --fields "name,state,host.name"
# stop one agent without affecting the others
mng stop agent-1
# the host stays running as long as at least one agent is active.

# TODO: many more examples of to add here of why this is useful!

##############################################################################
# RUNNING NON-AGENT PROCESSES
#   mng is useful for more than just AI agents! Run any long-lived process (like servers, data pipelines, etc.)
#   with mng to get the same benefits of easy management, logging, and remote execution.
##############################################################################

# run a Python script as a managed process
mng create my-server --command python -- -m http.server 8080

# run a long-running data pipeline
mng create etl-job --command python --idle-mode run --idle-timeout 60 -- etl_pipeline.py

# run a dev server with extra tmux windows for logs
mng create dev-env --command "npm run dev" -w logs="tail -f /var/log/app.log"

# use --idle-mode run so the host stops when the process finishes
mng create batch-job --provider modal --command bash --idle-mode run --idle-timeout 30 -- -c "python train.py && python evaluate.py"
# the container will be automatically snapshotted when completed, so you can later come back and connect (and start) to see the results:
mng conn batch-job

# TODO: lots more examples to create here! mng is basically a poor man's slurm/kubernetes/etc
#  there's really no need for most of those tools at all given mng (unless you're operating at a truly massive scale, which you are not)

##############################################################################
# SCRIPTING AND AUTOMATION
#   Use mng in shell scripts, CI pipelines, and cron jobs. JSON output,
#   headless mode, idempotent creation, and programmatic control.
##############################################################################

# run in headless mode (no interactive prompts)
mng create my-task --headless --no-connect --message "Do the thing"

# or set headless globally
mng config set headless true

# idempotent creation: reuse an existing agent if it already exists
mng create worker --reuse --provider modal --no-connect && mng message -m "Process the queue"

# get JSON output for parsing in scripts
AGENT_INFO=$(mng list --format json)

# use JSONL for streaming results into other tools
mng list --stream --format jsonl | while read -r line; do
  echo "$line" | python -c "import sys, json; d=json.load(sys.stdin); print(d.get('name', 'unknown'))"
done

# TODO: make examples of using "mng wait"

# TODO: make more examples here (observe, events, streaming, transcript, using jq and python, etc)

##############################################################################
# SETTING-ONLY OPTIONS
#   Some behavior can only be chnaged from the settings (not from the CLI)
#   These options are typically less commonly used or more advanced
##############################################################################


##############################################################################
# OUTPUT FORMATS AND MACHINE-READABLE OUTPUT
#   Switch between human-readable, JSON, and JSONL output. Use --format
#   with templates, pipe output to jq, and build tooling on top of mng.
##############################################################################

# default output is human-readable
mng ls

# use custom format templates to customize human-readable output for yourself
mng list --format '{agent.name} ({agent.state})'

# TODO: some of these commands are kind of duplicated...  what should we do about that?
#  perhaps a single test could point to multiple commands?

# JSON output (full array, good for programmatic use)
mng list --format json

# JSONL output (one object per line, good for streaming/piping)
mng list --format jsonl

# stream JSONL results as they arrive (don't wait for all results)
mng list --stream --format jsonl

# JSON and JSONL works with most commands
mng snapshot list --format json && mng plugin list --format jsonl

# combine json with jq for powerful filtering and transformation
mng list --format json | jq '.[] | select(.state == "RUNNING") | .name'

# combine jsonl with jq for streaming filtering
mng list --format jsonl | jq --unbuffered 'select(.state == "RUNNING") | .name'

##############################################################################
# UPLOADING FILES AND RUNNING SETUP COMMANDS
#   Upload files, append to configs, create directories, and run setup
#   commands on agent hosts during creation or via re-provisioning.
##############################################################################

# upload a file to the agent's host during creation
mng create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config

# run a setup command during host provisioning
mng create my-task --provider modal --extra-provision-command "pip install numpy pandas"

# run a command as root during provisioning (if your default user is not root, assumes passwordless sudo for that user)
mng create my-task --provider modal --extra-provision-command "sudo apt-get update && apt-get install -y vim"

# append content to a file on the host
mng create my-task --provider modal --append-to-file /root/.bashrc="export PATH=/opt/bin:\$PATH"

# combine multiple setup steps
mng create my-task --provider modal \
  --upload-file ./requirements.txt:/workspace/requirements.txt \
  --sudo-command "apt-get update && apt-get install -y build-essential" \
  --extra-provision-command "pip install -r /workspace/requirements.txt"

# TODO: also show how you can use "mng push" or "mng exec" after starting the agent, just as nice alternatives

##############################################################################
# ADVANCED WORKFLOWS
#   Complex multi-agent setups, custom scripts, and integrations with other
#   tools and platforms. Examples of building agent orchestration, custom dashboards, and more.
##############################################################################

# TODO: we should update the command here, see my examples from the blog post, etc

# auto-generated by Claude, remove when a human has sanctioned this
# fan-out pattern: create many agents from a list of tasks
for task in "fix-auth" "add-logging" "update-deps" "write-docs"; do
  mng create "$task" --provider modal --no-connect --message "Work on: $task"
done

# auto-generated by Claude, remove when a human has sanctioned this
# monitor all agents in a watch loop
mng list --watch 5 --running

# auto-generated by Claude, remove when a human has sanctioned this
# collect results from all agents
for agent in "fix-auth" "add-logging" "update-deps" "write-docs"; do
  echo "=== $agent ==="
  mng exec "$agent" -- git log --oneline -3
done

# auto-generated by Claude, remove when a human has sanctioned this
# snapshot all agents before a risky operation
mng snapshot create -a --name "pre-merge-checkpoint"

# auto-generated by Claude, remove when a human has sanctioned this
# batch cleanup: stop all agents, then destroy them
mng stop -a
mng destroy -a --force --remove-created-branch
mng gc

# TODO: there are a LOT more cool advanced workflows besides just map-reduce! Add a bunch more examples here

##############################################################################
# TIPS AND TRICKS
#   Power-user shortcuts, lesser-known features, and workflow patterns
#   that make working with mng faster and more pleasant.
##############################################################################

# use short forms for common commands to save typing
# mng c = mng create, mng ls = mng list, mng s = mng stop, mng destroy = mng rm
# mng conn = mng connect, mng msg = mng message, mng exec = mng x

# use --reuse to make create idempotent. This is handy, esp with remote scripts, so that you can detach, then hit up and enter
# and not have to worry about remembering whether it is started, etc (because it will attach by default)
mng create --reuse --provider modal my-task

# use watch with list to keep a live dashboard in a terminal
watch -n 5 mng list

# use exec to quickly inspect an agent's environment
mng exec my-task -- env | sort

# or use exec to see something across a bunch of hosts by combining with mng list:
mng list --include 'host.provider == "modal"' --ids | mng exec - 'echo $MNG_AGENT_ID && env | sort'

# if you want to get really fancy, you can use xargs to run in parallel across hosts:
mng list --include 'host.provider == "modal"' --ids | xargs -P 5 -I {} mng exec {} 'echo $MNG_AGENT_ID && pwd'

# check the transcript to see what an agent has been up to
# (helpful to see the last messages without even having to bring the host back online!)
mng transcript my-task --tail 5 --role assistant

##############################################################################
# TROUBLESHOOTING
#   Common problems and how to fix them. Debugging with logs, verbose
#   output, and exec. What to do when agents crash or hosts won't start.
##############################################################################

# TODO: finish off this section...

# auto-generated by Claude, remove when a human has sanctioned this
# check if the agent exists and its current state
mng list --fields "name,state,host.provider,host.name"

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
mng exec my-task -- cat /var/log/syslog | tail -20
mng exec my-task -- ps aux
mng exec my-task -- df -h

# auto-generated by Claude, remove when a human has sanctioned this
# if an agent is stuck, try stopping and restarting it
mng stop my-task
mng start my-task --connect

# auto-generated by Claude, remove when a human has sanctioned this
# if a host is in a bad state, destroy and recreate
mng destroy my-task --force
mng create my-task --provider modal

# auto-generated by Claude, remove when a human has sanctioned this
# garbage collect to clean up any orphaned resources
mng gc --dry-run
mng gc
