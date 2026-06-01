# Publish the agent-coordination Claude Code skills

`mngr extras claude-plugin` now installs more than just the code review
plugin. It offers two Claude Code plugins and lets you install either or
both:

- `imbue-code-guardian` -- automated code review enforcement (unchanged).
- `imbue-mngr-skills` -- the `message-agent`, `wait-for-agent`, and
  `find-agent` skills for coordinating mngr agents, now published from a
  marketplace hosted in the mngr repo itself.

With an interactive terminal the step shows a picker of the not-yet-installed
plugins (plus an "Install all" option); `mngr extras claude-plugin -y`
auto-installs every plugin that is not already present. `mngr extras` status
output reports each plugin's installed/not-installed state individually.
