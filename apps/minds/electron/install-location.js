'use strict';

// A packaged macOS app runs only from an Applications folder.
//
// Squirrel cannot replace a bundle on a read-only volume, which is where an
// app opened straight from its disk image runs, and so is one Gatekeeper has
// translocated (a quarantined app opened before Finder moved it runs from a
// hidden read-only copy). Such an install never updates, and says so nowhere
// a person sees. So the launch stops before anything starts and offers to
// move the app; there is no way to carry on from where it is.
//
// Free of any `electron` import so it can be unit-tested under plain node;
// main.js hands in Electron's `app` and `dialog`.

const { PRODUCT_DISPLAY_NAME } = require('./product-name');

const MOVE_BUTTON_INDEX = 0;
const QUIT_BUTTON_INDEX = 1;

/**
 * Whether this launch must stop and offer the move.
 *
 * `isInApplicationsFolder` is Electron's macOS-only check, read only for a
 * packaged macOS build: it accepts every Applications folder (~/Applications
 * too) and any path with a folder named Applications in it. ToDesktop's smoke
 * test cannot answer a dialog, so its launches are let through.
 */
function isLaunchRefused({ platform, isPackaged, isSmokeTest, isInApplicationsFolder }) {
  if (platform !== 'darwin' || !isPackaged || isSmokeTest) return false;
  return !isInApplicationsFolder();
}

/**
 * Ask to move the app into /Applications, and either move it or quit.
 *
 * A successful move quits this process and reopens the copy on its own. A
 * move that fails or is cancelled ends the launch too, with the reason and
 * the way to do it by hand.
 */
async function offerMoveToApplications({ app, dialog }) {
  const { response } = await dialog.showMessageBox({
    type: 'warning',
    message: `Move ${PRODUCT_DISPLAY_NAME} to your Applications folder`,
    detail:
      `${PRODUCT_DISPLAY_NAME} can't run from here. It needs to be in your Applications folder to ` +
      'install updates, and will reopen from there.',
    buttons: ['Move to Applications', 'Quit'],
    defaultId: MOVE_BUTTON_INDEX,
    cancelId: QUIT_BUTTON_INDEX,
  });
  if (response !== MOVE_BUTTON_INDEX) {
    app.quit();
    return;
  }
  let reason;
  try {
    // Electron documents a cancelled password prompt as `false`; its
    // implementation throws for that instead, so both are handled.
    if (app.moveToApplicationsFolder()) return;
    reason = 'The move was cancelled';
  } catch (err) {
    reason = err && err.message ? err.message : String(err);
  }
  console.error(`[install-location] could not move the app to Applications: ${reason}`);
  await dialog.showMessageBox({
    type: 'error',
    message: `Couldn't move ${PRODUCT_DISPLAY_NAME} to Applications`,
    detail: `${reason}. In Finder, drag ${PRODUCT_DISPLAY_NAME} into your Applications folder, then open it from there.`,
    buttons: ['Quit'],
  });
  app.quit();
}

module.exports = { isLaunchRefused, offerMoveToApplications };
