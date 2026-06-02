`to_offline_host` now returns an `OfflineHostWithVolume` (which implements the new
`HostFileReadInterface`) whenever the provider exposes a persistent volume for the host, via
the shared `make_readable_offline_host` helper. This makes a stopped host's files readable through the
same interface as an online host -- used by Claude session preservation when a host is
destroyed while offline, and available to other readers of offline host data. When no volume is
available, a plain (metadata-only) `OfflineHost` is returned as before.
