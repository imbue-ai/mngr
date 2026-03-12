# mng-tutor

Interactive tutorial for learning mng commands.

A plugin for [mng](https://github.com/imbue-ai/mng) that adds the `mng tutor` command. Launch with `mng tutor` in a separate terminal from your main working terminal.

## How it works

The tutor presents a menu of lessons, each with ordered steps. For each step:

1. Read the instructions in the tutor terminal
2. Run the suggested commands in your other terminal
3. The tutor automatically detects when the step is complete and advances

Completion is detected by monitoring agent state, filesystem changes, and tmux sessions -- no manual confirmation needed.

## Lessons

### Basic Local Agent

Learn to create, use, and manage your first agent locally:

- Create an agent with `mng create`
- Send commands via `mng message`
- Stop and restart the agent
- Destroy the agent when finished

### Remote Agents on Modal (WIP)

Learn to launch and manage agents on Modal's cloud infrastructure:

- Create a remote agent with `--in modal`
- Work with the remote agent
- Stop, restart, and destroy

## Tips

- Run the tutor in a separate terminal window, not a tmux pane, to avoid confusion with the agent's tmux session
- You can skip `mng start` and just run `mng connect` directly -- it starts the agent first if needed
- Press Ctrl-T or Ctrl-Q within an agent's tmux session as shortcuts for `mng stop` and `mng destroy`
