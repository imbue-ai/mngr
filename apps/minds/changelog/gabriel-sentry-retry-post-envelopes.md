The dev-time desktop bundle lock (`electron/pyproject/uv.lock`) records `urllib3` as a declared
dependency of `imbue-common`, which this project resolves by path. `imbue-common` imports urllib3
directly to configure the Sentry transport's retries. No resolved package version changed.
