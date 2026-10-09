'use strict';

// The name a person reads: window titles, the menu bar, notifications, the
// loading and error screens. Distinct from package.json's productName, which
// is the file-system name (the macOS bundle, the Linux /opt directory,
// Electron's scratch directories) and so carries no space. Free of any
// `electron` import so the pure modules and their unit tests can read it.
const PRODUCT_DISPLAY_NAME = 'Imbue Studio';

module.exports = { PRODUCT_DISPLAY_NAME };
