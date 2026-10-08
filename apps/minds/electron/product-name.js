'use strict';

// The name a person reads: window titles, the menu bar, notifications, the
// loading and error screens. Separate from package.json's productName, the
// file-system name (the macOS bundle, the Linux /opt directory, Electron's
// scratch directories), so the copy the app writes and the paths it is
// packaged under move independently. Free of any `electron` import so the
// pure modules and their unit tests can read it.
const PRODUCT_DISPLAY_NAME = 'Imbue Studio';

module.exports = { PRODUCT_DISPLAY_NAME };
