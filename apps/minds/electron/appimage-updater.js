'use strict';

// electron-updater's AppImage updater, minus its restart.
//
// Its install replaces the AppImage file and then starts the replacement at
// once, before this process has quit. The new instance fails the
// single-instance lock and exits, and its arrival focuses this window over
// the running-workspaces prompt of the quit that is under way, so the prompt
// looks like a hang and nothing comes back once it is answered. The app
// starts the replacement itself once this process has exited (see
// linux-relaunch.js), as it does for the .deb.
//
// Free of any `electron` import beyond what electron-updater loads lazily, so
// it is unit-testable under plain node with a fake app adapter (see
// ../test/unit/appimage-updater.test.js).

const { AppImageUpdater } = require('electron-updater');

class AppImageUpdaterWithoutRestart extends AppImageUpdater {
  /**
   * The install keeps the file replacement but not the start. Of the two
   * branches after the replacement, only the "run after" one hands the start
   * to `spawnLog`; the other runs the replacement synchronously and waits for
   * it to exit.
   */
  doInstall(options) {
    return super.doInstall({ ...options, isForceRunAfter: true });
  }

  /** The start electron-updater would give the replacement: not here. */
  spawnLog() {
    return Promise.resolve(true);
  }
}

module.exports = { AppImageUpdaterWithoutRestart };
