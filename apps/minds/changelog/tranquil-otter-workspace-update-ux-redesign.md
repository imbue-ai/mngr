Workspace updates move out of the way of the workspace.

- A small status pill in the titlebar, after the workspace's icons, now carries a workspace's update: orange when it needs you ("Update available" is grey with an orange dot), and it opens the update chat or the Updates tab. Following "Update didn't finish" or "Updated, with a note" to the chat dismisses it. The full-width band no longer shows update notices, so the workspace keeps its full height. The pill hides while the band reports a connection problem and while an update is applying. A scheduled update shows no pill and no home-screen badge; the Updates tab says it is scheduled.

- The update popup is gone. The pill, home badges and failed presses open the workspace's Settings -> Updates tab, which is rebuilt to say everything the popup did: a status block with the version jump, the security-fix card, what an update costs, the actions, and "Update to a specific version". How the last run ended (didn't start, didn't finish, updated) has no Dismiss button: leaving the tab dismisses it. A run's chat is offered wherever Studio can find it, including a run started by hand in a chat; when it can't, nothing tells you to go to the chat.

- Auto-update is now per workspace: each workspace follows Studio settings, auto-updates in its own window, or updates only by hand. Studio settings lists the workspaces with their own setting. The "Update all" strip counts only workspaces auto-update won't handle. A release whose run failed or stalled is not scheduled again by auto-update until a newer release is out; dismissing the failure no longer re-arms it, and the workspace says it is behind meanwhile.

- Copy: "Update didn't finish", "Update needs a decision", "your local time", "Studio settings", and tokens for cost.
