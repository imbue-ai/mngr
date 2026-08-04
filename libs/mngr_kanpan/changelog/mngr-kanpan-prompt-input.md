Custom kanpan commands can now ask you for a value before they run, so free-form per-agent input (a note, a rename, a message) is config rather than one keybinding per possible value:

- Set `prompt` on a `[plugins.kanpan.commands.<key>]` entry to float a small input in the middle of the board when the key is pressed, titled with the agent it will act on. The text you type reaches the command as the `MNGR_INPUT` environment variable, alongside the existing `MNGR_AGENT_NAME`. The input has the same readline editing as the peek reply, and the board stays visible around it.

- `Enter` runs the command, including on an empty line (that is how you clear a value); `Esc` or `Ctrl-C` cancels and runs nothing. The target agent is captured when the prompt opens, so a refresh landing while you type cannot retarget the command. Prompted commands carry a trailing `…` wherever their key is listed -- the `?` overlay, and the footer belt in the case where the command overrides one of the keys the belt advertises.

- The value is passed through the environment rather than interpolated into the command string, but the command still runs under a shell, so quote `"$MNGR_INPUT"` in your command.

- Prompted commands run on their own worker, so a command against an unresponsive host can no longer hold up a board refresh.

- `prompt` combined with `markable` prompts once for a whole batch: mark agents with the key, press `x`, and the value you type is applied to every marked agent. Several prompted commands marked at once are asked for in turn, one prompt each, before anything runs; cancelling any of them runs nothing at all and keeps the marks. A retry after a failure asks again rather than reusing the earlier answer.

- Label-backed columns now blank their cell as soon as the label is cleared, instead of showing the old value until the next full refresh. Note that `mngr label` has no delete path, so clearing a label sets it to the empty string: the cell blanks but presence filters such as `has(labels.note)` keep matching. In `--format json`/`--format jsonl` this means a label-backed column's key is now always present in an agent's `fields` and `cells`, carrying an empty value for agents that lack the label, where it was previously omitted -- so test the value rather than the key's presence.

The README documents `prompt`/`MNGR_INPUT` and adds a worked example that combines a prompted command with a label-backed column to keep a note against each agent -- with the caveat that a label holds a single value, so this is one note rather than a set of tags, and a small fixed vocabulary is better served by a key per value than by a prompt.

While the prompt, the peek panel, or the `?` overlay is open, clicks on the board are ignored rather than moving the selection out from under the panel. (A click during a `/` search still ends the search on the row clicked, as before.)
