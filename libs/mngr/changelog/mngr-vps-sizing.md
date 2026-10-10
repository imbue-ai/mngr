- The shared provider release trip (`run_provider_release_trip1`) gains an optional size check: a profile that implements `expected_host_resources` has the size `mngr list --format json` reports for the created host compared against what the cloud itself says. The `host_from_list_json` / `host_resources_from_list_json` / `assert_host_resources_match` helpers are shared with the provider-specific release tests.

- The hosts concept doc ("Sizing") notes that the cloud VPS providers record and report the instance shape the cloud reports, and list an unanswerable size as unknown rather than as a placeholder.

- The generic listing path reads an online host's recorded size the same guarded way it already read an offline host's: a provider whose record cannot answer the size (e.g. a VPS record from before shapes were recorded) lists the host with no `host.resource` instead of failing its row.
