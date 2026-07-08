Fixed the workspace-recovery flow stranding users on a "Workspace unresponsive" page after their computer wakes from sleep, even when the workspace was healthy the whole time.

Five changes to the recovery/probing logic:

- The recovery page now runs a cheap liveness poll from the moment it opens and keeps it running under every state (including "Workspace unresponsive" and after a failed restart). The instant the workspace answers, the page returns you to it -- so a workspace that comes back on its own (e.g. after a wake) no longer leaves you stranded on a static verdict page.

- A workspace health probe that times out is now treated as "no answer yet, keep checking" rather than as proof the workspace is down. A probe interrupted by the machine sleeping no longer produces a false "unresponsive" verdict.

- The recovery page can now appear promptly when a workspace stops responding, instead of waiting for fresh discovery data first. The freshness check moved to where it actually matters -- deciding whether to show a restart verdict or auto-restart -- so a healthy workspace returns you home fast, while a genuinely-broken one still waits for trustworthy data before offering a restart. When that data hasn't arrived yet (or the probe timed out) the page shows a live "Reconnecting to your workspace" state that self-heals, rather than an indefinite loader with no recourse.

- When the recovery page's own health request is dropped mid-flight -- most often because the machine slept and the browser aborted the in-flight fetch -- the page now shows the live "Reconnecting to your workspace" state and retries the probe, instead of dead-ending on a static "Workspace unresponsive" verdict. A dropped request is absence of an answer, not proof the workspace is down, so the page keeps checking and returns you home the instant the workspace responds.

- The workspace health probe now re-reads the host's state at the moment it decides whether its classification is trustworthy, instead of using the state read before its slow in-container check. Previously, a discovery update landing during that check could open the freshness gate for a stale reading -- rendering a consent-gated "Workspace unresponsive" verdict (host supposedly still running) when the fresh data already showed the host fully stopped, a state that should instead trigger an automatic unattended restart.
