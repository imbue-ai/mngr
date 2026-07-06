Fixed the workspace-recovery flow stranding users on a "Workspace unresponsive" page after their computer wakes from sleep, even when the workspace was healthy the whole time.

Three changes to the recovery/probing logic:

- The recovery page now runs a cheap liveness poll from the moment it opens and keeps it running under every state (including "Workspace unresponsive" and after a failed restart). The instant the workspace answers, the page returns you to it -- so a workspace that comes back on its own (e.g. after a wake) no longer leaves you stranded on a static verdict page.

- A workspace health probe that times out is now treated as "no answer yet, keep checking" rather than as proof the workspace is down. A probe interrupted by the machine sleeping no longer produces a false "unresponsive" verdict.

- The recovery page can now appear promptly when a workspace stops responding, instead of waiting for fresh discovery data first. The freshness check moved to where it actually matters -- deciding whether to show a restart verdict or auto-restart -- so a healthy workspace returns you home fast, while a genuinely-broken one still waits for trustworthy data before offering a restart. When that data hasn't arrived yet (or the probe timed out) the page shows a live "Reconnecting to your workspace" state that self-heals, rather than an indefinite loader with no recourse.
