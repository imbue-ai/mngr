A claude agent whose turn ended on an API error -- a usage limit, a rate limit, a prompt too long -- now reports WAITING instead of staying RUNNING forever.

Claude Code has two mutually exclusive turn-end paths. A turn that finishes normally fires `Stop`; a turn that dies on an API error fires `StopFailure` and returns before the `Stop` pass ever runs. mngr registered only `Stop`, so the `active` marker that `UserPromptSubmit` created was never removed and the lifecycle probe kept reporting the agent as RUNNING until its claude process restarted. Nothing else cleared it: the usage-limit selector fires no `PermissionRequest`, and Claude Code suppresses its `idle_prompt` notification while a dialog is on screen or quota auto-resume is armed.

The effect was visible anywhere lifecycle state is read. In the minds app, a workspace whose update agent hit a usage limit sat on "Preparing update..." indefinitely, and the wedged run slot refused any retry.

`StopFailure` now runs the same turn-end script as `Stop`, so the transcript flush still lands ahead of the cleared marker and a consumer woken by the turn-end signal sees the error that ended the turn. Agents pick this up when they are next provisioned.
