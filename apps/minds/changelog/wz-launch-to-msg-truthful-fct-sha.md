launch-to-msg slack message now reports the FCT SHA the e2e actually
exercised (re-resolved right before the .app launches) instead of the
SHA `check_should_run` computed at workflow-start time. Also: trigger
line now surfaces who initiated the run and on which ref --
`schedule on main` for the twice-daily cron vs.
`workflow_dispatch by @<actor> on <branch>` for a manual dispatch.
