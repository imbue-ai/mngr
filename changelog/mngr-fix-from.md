Fix `mngr create --from @HOST.PROVIDER:PATH` (e.g. `--from @m1.modal:/some/path`),
which previously failed with "Could not find host with ID or name: HOST.PROVIDER".
The same fix also lets `mngr limit --host` and other host-name-or-ID inputs accept
the `host.provider` disambiguation form.

Also fix `mngr create --from` against a remote source: the current-branch lookup
previously raised `NotImplementedError` for non-local hosts and now runs via the
host interface so it works for any provider.
