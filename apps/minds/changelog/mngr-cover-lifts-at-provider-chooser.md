After a create, the new workspace shows up sooner, and with a template that supports it, the AI-provider chooser appears without waiting for the welcome chat.

- The wash into the workspace starts as soon as the create is done, with "You're all set." streaming underneath it, rather than after the line has finished. The wash also covers the window faster (500 ms rather than 864 ms), and the workspace starts loading under it at 560 ms rather than 950 ms. Under reduced motion the cover is instant, as before.

- Embed contract v16: a new workspace-to-embedder message, `minds:provider-chooser-on-screen`, and a `reportsProviderChooserOnScreen` field on `minds:workspace-ready`. `minds:focus-chat` can now be sent without a `chatId`, which asks the workspace to show its chat root window.

- When a new workspace's template announces `reportsProviderChooserOnScreen`, the creation hand-off asks it to show its chat window as soon as it is ready. The wash then lifts once the provider chooser is on screen, without waiting for the welcome chat's seed, and the welcome chat is still brought forward once it exists. `minds.log` records this case as `PROVIDER_CHOOSER_ON_SCREEN` at debug level. With an older template nothing changes: the wash waits for the welcome chat, still with the 45-second limit.

- The end-to-end runner's provider-chooser sign-in (`sign_in_via_provider_chooser`, used by the launch-to-message run and the snapshot-resume test) finds the chooser in the chat list that frames the welcome chat, where the paired template opens it, as well as in the chat's own page, where older templates open it.
