The kanpan header can now carry a line of your own text on its right, built from counts over the agents on the board:

```toml
[plugins.kanpan]
header_status = '{state == "RUNNING"} running / {total}'
```

Each braced CEL expression renders as the number of agents it holds for -- the same language `--include` takes, evaluated against the entry shape `--format json` emits, so a count can be taken against any board column (`cells.ci.text == "failure"`, `fields.pr.state == "OPEN"`, `section == "PR_MERGED"`) and not just the agent state. `{total}` counts every agent, and `{{`/`}}` are literal braces. Counts run over the agents the board is showing, so they follow any active filter and any custom `section_order`.

The title stays centred on the screen whatever the status says, and a status too wide for the space beside it is dropped whole rather than clipped into a fragment of itself. An unbalanced brace, or an expression that is not valid CEL, is reported when kanpan starts rather than once the board is up.
