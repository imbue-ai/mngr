# imbue-mngr-tutor

Interactive tutorial for learning mngr commands.

A plugin for [mngr](https://github.com/imbue-ai/mngr) that adds the `mngr tutor` command. Launch with `mngr tutor` in a separate terminal from your main working terminal.

## How it works

The tutor presents a menu of lessons, each with ordered steps. Read the instructions in the tutor terminal, run the suggested commands in your other terminal, and the tutor automatically detects when each step is complete and advances -- no manual confirmation needed.

## Lessons

### Basic Local Agent

Learn to create, use, and manage your first agent locally: create an agent with `mngr create`, send commands via `mngr message`, stop and restart it, and destroy it when finished.

A lesson on remote agents on Modal (`--in modal`) is in progress and not yet available.

## Tips

- Run the tutor in a separate terminal window, not a tmux pane, to avoid confusion with the agent's tmux session
- You can skip `mngr start` and just run `mngr connect` directly -- it starts the agent first if needed
- Press Ctrl-T or Ctrl-Q within an agent's tmux session as shortcuts for `mngr stop` and `mngr destroy`
