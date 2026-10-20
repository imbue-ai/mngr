Witnessed the `mngr file` behavior corpus: every unit in `libs/mngr_file/behaviors/` now has at
least one test linked back to it, and the divergences those tests found in `mngr file` are fixed.

- New witnessing tests under `libs/mngr_file/imbue/mngr_file/witnesses/`, one module per corpus
  area: `addressing/targets_test.py`, `addressing/path_resolution_test.py`,
  `addressing/stopped_hosts_test.py`, `addressing/invariants_test.py`, `listing/listing_test.py`,
  `transfer/reading_test.py`, `transfer/writing_test.py`, and `invariants_test.py` for the
  corpus-wide Rules. Each drives the real `file get`, `file put`, and `file list` commands and
  carries a `witnesses` marker naming the unit it witnesses, with a `partial=` note wherever it
  covers less than the unit states.

- `get` no longer escapes an uncaught error for a missing path or a directory: it establishes what
  is at the path through the host interface before reading, so the refusal is the same stated line
  whether the machine is the running local host, a stopped host served from a filesystem volume, or
  one served by a storage service that raises errors of its own kind
  (`transfer.no-file-at-path`, `transfer.path-is-a-directory`,
  `transfer.directory-refusal-is-machine-independent`, `clean-refusals`).

- `list` refuses a path that names a file instead of reporting it as an empty directory, and a
  missing directory on a stopped host served by a storage service is now refused cleanly rather
  than escaping that service's own error (`listing.missing-directory`, `clean-refusals`).

- `list` classifies a named path before listing it, so listing a file on a stopped host served by a
  storage service is refused with a stated error instead of crashing (`clean-refusals`,
  `resolved-path-reported`).

- `put` refuses writing onto a directory or beneath a file, refuses `--input` naming a directory,
  and its missing-content refusal names both ways of offering content, followed by worked examples
  (`transfer.no-content-offered`, `clean-refusals`).

- Refusal witnesses now require one stated `Error:` line and allow guidance after it, instead of
  requiring the whole refusal to fit on one line (`clean-refusals`, `transfer.no-content-offered`).

- Lifecycle witnesses now read, write, and list with missing files, `--mode`, and `--recursive`, by
  host target and by each agent base, on every storage kind and on running hosts
  (`addressing.never-changes-lifecycle`); resolution witnesses cover absolute paths by host target on
  running and stopped hosts (`addressing.same-resolution-everywhere`).

- The `get` template refusal is also witnessed on a path with no file behind it, so a read before
  the refusal is detected (`template-refused-where-the-outcome-is-content`); put's report test no
  longer claims `named-output-formats`, which the invariants witness covers.

- `get --output` refuses a local destination that is a directory or lies beneath a file, and
  reports the saved local path and the path read as absolute paths
  (`transfer.saved-to-local-file`, `resolved-path-reported`).

- `--relative-to state` on a host target is now a usage error naming the option rather than a
  silently reinterpreted base (`addressing.state-base-needs-an-agent`).

- Test scaffolding added in `testing.py` and `conftest.py`: CLI-discoverable stopped hosts whose
  persisted storage is a filesystem, a storage service, or unreachable (`stopped_host_factory` and
  its `stopped_host`, `service_stopped_host`, and `unreachable_stopped_host` fixtures), a
  `local_agent` fixture, `write_agent_record`, `read_tree`, `InteractiveStdin`, and one
  `addressed_machine_factory` that names the machine a command addresses -- the running local host
  or a fresh stopped host reached through storage -- which replaced three separate copies the
  area branches had each grown.
