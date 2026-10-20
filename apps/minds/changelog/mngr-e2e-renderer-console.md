The `launch-to-msg` e2e now captures the console errors and warnings the workspace pages emit, along with any exception they leave uncaught, so a UI failure can be diagnosed from the CI artifacts.

Electron forwards only its main process's console to `electron.log` -- artifacts carry `[backend]`, `[lifecycle]`, `[nav]`, `[startup]` and nothing from the pages themselves. So when the app reported a failure of its own (`MessageInput.ts` logs `console.error` on a failed send, for instance), the message reached no artifact and the run could only be diagnosed by inferring from screenshots.
