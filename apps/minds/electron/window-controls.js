// Which window controls a window's bar shows: the native macOS traffic lights
// (the bar leaves room for them), traffic lights the bar draws itself, or the
// bar's own minimize / maximize / close buttons.
//
// Deliberately free of any `electron` import so the rule is unit-testable
// under plain node. Main resolves it once at startup and hands it to every
// window's preload as a command-line switch, since a sandboxed preload can
// require neither this module nor read main's environment.

const WINDOW_CONTROLS_ENV_VAR = 'MINDS_WINDOW_CONTROLS';
// The one value the variable accepts: a window off macOS made to look like one
// (the demo box records the app on a mac-styled Linux desktop).
const DRAWN_MAC_ENV_VALUE = 'mac';
const WINDOW_CONTROLS_SWITCH = 'minds-window-controls';

const NATIVE_MAC = 'native-mac';
const DRAWN_MAC = 'drawn-mac';
const BUTTONS = 'buttons';

/**
 * The window controls for this launch, from the platform and the environment.
 *
 * Throws on a value the variable does not accept rather than falling back to
 * the platform's controls, so a misspelt setting cannot silently record a
 * demo with the wrong chrome.
 */
function resolveWindowControls(platform, env) {
  if (platform === 'darwin') return NATIVE_MAC;
  const requested = env[WINDOW_CONTROLS_ENV_VAR];
  if (requested === undefined || requested === '') return BUTTONS;
  if (requested === DRAWN_MAC_ENV_VALUE) return DRAWN_MAC;
  throw new Error(
    `${WINDOW_CONTROLS_ENV_VAR}=${JSON.stringify(requested)} is not a window style; ` +
      `unset it, or set it to ${JSON.stringify(DRAWN_MAC_ENV_VALUE)}.`
  );
}

/** The command-line switch a window's preload reads the resolved controls from. */
function windowControlsSwitch(windowControls) {
  return `--${WINDOW_CONTROLS_SWITCH}=${windowControls}`;
}

module.exports = {
  BUTTONS,
  DRAWN_MAC,
  NATIVE_MAC,
  WINDOW_CONTROLS_ENV_VAR,
  WINDOW_CONTROLS_SWITCH,
  resolveWindowControls,
  windowControlsSwitch,
};
