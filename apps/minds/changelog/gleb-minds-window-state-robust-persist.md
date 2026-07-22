Fixed a bug where restarting the app -- especially via the "Install and Restart" auto-update prompt -- could drop you on the "create a new workspace" screen instead of restoring your open workspace windows.

The desktop shell now persists its window layout continuously (debounced) as you move, resize, and navigate windows, instead of only at quit. So however the app exits (auto-update restart, crash, or force-quit), the saved layout reflects your live windows and is restored on the next launch.

It also no longer lets a non-graceful quit -- which tears windows down while the save is running -- overwrite a good saved layout with an empty one, which was the direct cause of landing on the create screen.

The startup log now records why the app chose its landing screen (authenticated, account/workspace counts, restorable-window count, and the chosen route), making a bad restore diagnosable from `~/.minds/logs`.
