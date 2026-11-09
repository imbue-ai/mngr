# The workspace command contract

Version: 1 (tracks `CONTRACT_VERSION` in
`apps/minds/imbue/minds/workspace_programs/workspace_command_contract.py`)

Imbue Studio runs scripts that live in the workspace's own copy of
default-workspace-template (`system/scripts/message_chat.py`,
`seed_welcome_chat.py`, ...). That copy belongs to the user, and the user's
agents edit it, so an edit there can break the app. This contract is the list
of what the app runs there, and both repos check it.

## One declaration, checked from both sides

`workspace_command_contract.py` declares every command the app, or one of its
workspace programs, runs from the template:

- the file or console script, and how it is run (`python3 <path>` with no venv,
  or `uv run --no-sync <name>` from the root venv);
- every argument shape the app passes (subcommand, options, positional count, and the
  values it passes verbatim to an option or argument that takes a fixed set);
- the exit statuses the command answers with and what each means (the app
  need not tell every one apart), and the output it parses;
- the template paths the app only checks for (`REQUIRED_PATHS`), such as a
  skill's `SKILL.md`.

It is a standard-library module, so the workspace programs (which ship into the
workspace as zipapps) import it as the app does. `workspace_command_contract.json`
beside it is the same declaration as data, the file the template reads; a unit
test keeps the two equal.

**The app side.** The app builds each command's arguments with the
contract's builders (`message_chat_send_arguments`, `command_argv`, ...).
`desktop_client/workspace_command_contract_test.py` checks the argument shapes the
builders and the app's public command builders produce against the declared
invocations in both directions (each built shape is declared, and each declared
invocation is built), so a change to what the app sends fails until the
declaration says it too.

**The template side.** `system/scripts/fetch_mngr_assets.sh` vendors the JSON file from
the mngr commit the template pins, as it does the embed contract. The template's
`system/test_studio_command_contract.py` runs every declared command for real:
it checks that the file exists and runs under its declared runner, that `--help`
lists every subcommand, option and fixed value the app passes (for a script that
parses its own arguments, that its source still reads each option), that an argument the script
does not know exits with the status the app reads as "older template", and that
every required path exists. It lives directly in `system/`, so it is in the
always-run set of `app-manifest select-tests`: every change a workspace agent
makes runs it. Its failure message says the command is Imbue Studio's
interface to the workspace, not the workspace's to change. The template's
`AGENTS.md` and the scripts' own docstrings say the same.

**At runtime.** When `message_chat.py` is present but argparse rejects the
arguments the contract declares, the app's callers (`chat_app.ask_chat_app` and the
`/assist` launch in `skill_chat.launch_skill_chat`) fall back as before, and also log
a warning that names the script and the contract version
(`chat_app.warn_if_arguments_rejected`). The update dispatcher does the same for
its update-chat create, in the workspace's `dispatcher.log`. This
happens when a template predates an invocation or an edit broke the script,
and without the warning the fallback hides it.

## Changing it

- **Adding a command, an invocation, or an option.** Declare it, build it
  through a builder, and land the template side in a paired default-workspace-template
  branch (`dwt-mngr-pin.md`). The template's guard fails against the new pin until
  the template accepts it. No version bump: the structural checks see it.
- **Changing what an existing command must do.** This covers an exit status's
  meaning or an output's shape. Bump `CONTRACT_VERSION` and
  update this file. The structural checks cannot see these changes. The template's
  guard pins the version it was last reviewed against, so the bump fails the
  template until someone re-checks its scripts and moves the pin.
- **Older templates.** The app still runs workspaces on every template release back to
  update-self's floor, and keeps its fallbacks (`CLEANUP:` comments) for the
  ones that predate a command. The contract describes the current template, and
  the fallbacks cover the older ones.

## Not covered yet

The inventory behind this contract also found surfaces the app depends on
that are not commands, and that version 1 leaves out:

- files the app reads or writes by path: `.mngr/settings.local.toml`'s
  `[commands.create].type`, `data/.state/update-apply/run.json`,
  `data/.state/share_gateway/status.json`, `data/.secrets/share_grants.toml`,
  `data/system/backup.toml`, the background-task marker files, and the update-self
  merge subject `workspace_version.py` reads (`data/.state/profile/account.json`
  already has its own contract, `workspace_profile_contract.json`);
- the `chat` create template in `.mngr/settings.toml` that every fallback
  `mngr create` names, and the agent types the laptop seeds;
- `tk close`, `supervisorctl` and the `host-backup` program name in the backup programs;
- the stderr phrases the update dispatcher matches on a refused create
  (`"already exists; pick another name"`, `"is still being created"`, ...), which come
  from the chat app through `message_chat.py`;
- `system/scripts/agy_shim` (the antigravity plugin) and
  `system/scripts/env.d/2000-screen.sh` (the imbue_cloud pool bake), run by mngr
  plugins rather than the app.
