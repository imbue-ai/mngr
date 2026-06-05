Test-quality fixes in the `imbue_cloud` plugin's test suite (no user-facing behavior change):

- `pool_hosts` INSERT column check now compares against the parsed column tokens instead of raw substrings, so dropping a required column whose name is a substring of another (`id`, `ssh_port`) actually fails the test.
- Removed dead, misleading `_make_client` test scaffolding in the connector-client tests and routed the remaining lease/auth tests through the shared `MockTransport` helper (cuts the `monkeypatch.setattr` count from 5 to 1).
- The lease-release-on-failure test's recording client now inherits the real `ImbueCloudConnectorClient` interface (so `release_host` signature drift is caught), and the provider stub's cleanup recorder uses a per-instance `PrivateAttr` list instead of a shared class-level default.
- Documented the `bucket`/`paid` CLI help-text tests as intentional wiring smoke tests.
