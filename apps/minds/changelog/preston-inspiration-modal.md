Inspiration deeplinks (`minds://create?git_url=...`) now adapt to where you are. **When you're already inside a machine**, the deeplink opens a popup over it instead of yanking you to a full page, and you click through it in place:

- **Add to {this machine}** walks you through copying the `/use-inspiration <repo>` message and then tells you to paste it into that machine's chat. Since you're already in the machine you'd be adding to, it skips the machine picker entirely. That last step waits for you to click **Done** -- it never closes on its own, so the instruction can't disappear before you've read it.

- **Create a new machine** opens the full Create from Inspiration page (already past the first question), since setting up a new machine is more than a popup should hold.

**When you're not in a machine** (the home screen), the deeplink opens the full Create from Inspiration page exactly as before, picker and all.
