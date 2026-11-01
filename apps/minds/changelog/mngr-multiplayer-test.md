# Share settings for the whole workspace (embed contract v13)

`minds:open-share-settings` now takes an optional `serviceName`: a message
without one opens the workspace-options panel's Share tab on the whole
workspace rather than on one app, which is what the workspace shell's tray
share button (the multiplayer refinements in default-workspace-template) is to
send.
A present name is still held to `SERVICE_NAME_PATTERN`; an empty string is
refused as before. A desktop from before v13 (v12 added the
Screen's immersive mode) drops the nameless message.
