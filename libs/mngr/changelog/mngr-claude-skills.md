# Install the agent-coordination Claude Code skills

`mngr extras claude-plugin` now installs more than just the code review
plugin. It offers two Claude Code plugins and lets you install either or
both:

- `imbue-code-guardian` -- automated code review enforcement (unchanged).
- `imbue-mngr-skills` -- the `message-agent`, `wait-for-agent`, `find-agent`,
  and `mngr-help` skills for working with mngr, published from the dedicated
  `imbue-ai/mngr-claude-skills` repo.

With an interactive terminal the step shows a checkbox picker of the
not-yet-installed plugins (all preselected; Space toggles, Enter confirms),
matching the multi-select UI of the `mngr extras plugins` wizard;
`mngr extras claude-plugin -y` auto-installs every plugin that is not already
present. `mngr extras` status output reports each plugin's
installed/not-installed state individually.
