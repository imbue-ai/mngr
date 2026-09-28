The app calls itself Imbue Studio everywhere a person reads it.

The previous rename set the app's identity -- the bundle, the menu bar, the About panel, the window title -- but left the in-app copy saying "Mind", and in places the older "Minds". The settings panes, the update and recovery cards, the permission and credential dialogs, the notification copy, the folder-sync copy, and the sign-in page now all say Imbue Studio.

The file-system name is unchanged and still carries no space: the bundle, the Linux install directory, and Electron's own directories keep `ImbueStudio`.

The end-to-end harness reads the backend's login-URL log line by name, and accepts the old and new spellings both, so it works against a build from either side of the rename.

A second pass caught the copy the first sweep could not see. The create-progress log the user watches while a workspace is built prefixed all 24 of its lines with `[minds]`, and nothing strips that prefix before the page renders it. The Linux installer's own output -- including the final "minds is installed." line, which no word-boundary search can find because the ANSI colour code's trailing `m` abuts it -- the `minds --help` summary, the device-id and data-directory errors, the delete-account confirmation, the notification-settings instructions, the wordmark's accessible name, and the artwork attribution notice shipped with the app all said the old name too.

The behavior corpus said one thing while the app said another: `home-page.feature` asserted the user sees a "Help improve Mind" consent screen that the app had already renamed. The corpus and its READMEs now match the shipped copy.

One factual error is fixed rather than renamed: the API documented an omitted workspace name as auto-assigned `mind-N`, but the default has been `workspace-N` since the host-name base changed. That description is published in the OpenAPI document served at `GET /api/schema`.
