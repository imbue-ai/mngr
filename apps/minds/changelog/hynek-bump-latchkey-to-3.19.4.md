Bump Latchkey to 3.19.4.

The electron-updater / builder-util-runtime alignment test now accepts pnpm peer-dependency suffixes (for example `electron-updater@6.8.9(supports-color@7.2.0)`) on lockfile snapshot keys and resolved versions, instead of failing whenever a re-resolve links an optional peer somewhere below electron-updater.
