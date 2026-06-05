Creating a Modal host with an invalid argument (e.g. a non-existent
`--snapshot` image id) now fails with a clean single-line error
(`Error: Failed to create Modal host: '<id>' is not a valid Image ID.`) instead
of dumping a raw Python traceback. `create_host` now wraps
`ModalProxyInvalidError` in a user-facing `MngrError`, mirroring how
`ModalProxyRemoteError` was already handled.
