Strengthened low-quality tests under the Docker provider so they fail on real
regressions:

- `_list_containers` discovery now asserts an exact count and the exact set of
  created container names (catches a broken label/prefix filter that
  over-collects).
- The detached-exec test now measures wall-clock time to prove it returns
  without blocking.
- The entrypoint test now asserts PID 1's cmdline contains the real
  `CONTAINER_ENTRYPOINT_CMD` (trap + `tail -f /dev/null & wait`) instead of a
  bare `"sh"` substring.
- `get_files_for_deploy` deploy-file tests now prove the SSH-key exclusion
  filter discriminates (a non-excluded `config.json` is collected while the key
  files are dropped) and assert the `~/`-prefixed destination key.
- The Docker backend `build_provider_instance` test asserts the constructed
  instance wires through the given config and mngr context.
- Moved `_save_failed_host_record` verification from a daemon-backed acceptance
  test to a deterministic unit test (LocalVolume-backed store).
- Removed redundant raw-Docker-SDK self-tests (`docker commit` round-trip and
  bare `container.stop/start`); equivalent provider-level behavior is covered by
  the lifecycle and state-transition acceptance tests.
- Added intent/cross-reference comments to the capability-property contract
  tests.

Strengthened low-quality tests under the shared provider utilities
(`deploy_utils`, `ssh_host_setup`, `listing_utils`, `registry`):

- `detect_mngr_install_mode` now has deterministic coverage of the editable /
  non-editable / malformed-JSON / missing branches via a new pure helper
  (`_install_mode_from_direct_url_text`), replacing assertions that passed
  regardless of the returned value. (Behavior-preserving production refactor:
  extracted the direct_url.json -> install-mode mapping into the new helper;
  `detect_mngr_install_mode`'s public behavior is unchanged.)
- The deploy-file merge test now asserts the merged source values survive, and
  the enum test is now a round-trip (`MngrInstallMode("AUTO") == AUTO`) instead
  of restating the framework-generated value.
- SSH host-setup command tests now assert the security-critical content:
  the client key lands in `authorized_keys`, the host private key is installed
  at `/etc/ssh/ssh_host_ed25519_key`, `rm -f /etc/ssh/ssh_host_*` precedes the
  key write, the `chmod 600`/`644` permissions are set, and single quotes in a
  key are shell-escaped. The resource-loader and regular-user path tests use
  precise tokens instead of loose `or`/over-broad negative substrings.
- `parse_listing_collection_output` now has coverage for the container-state
  lines (`CONTAINER_STATE`/`CONTAINER_EXIT_CODE`/`CONTAINER_MISSING`), exclusion
  of agent blocks lacking a data section, and `certified_data` population. The
  generated listing script is exercised end-to-end against a fake host_dir tree
  via bash and round-tripped through the parser. Added `parse_optional_int`
  boundary cases (float string -> None, whitespace-padded int).
- Registry help-section tests now fetch the expected build/start help from the
  backend classes (the source of truth) instead of pinning verbatim sentences.

Strengthened low-quality tests under the local and SSH providers:

- Local/SSH `get_host_resources` tests now assert the actual reported values
  (real `psutil` CPU/memory for local; the SSH provider's hard-coded defaults)
  instead of loose `>= 0` bounds, and the SSH test uses a real host instead of
  an inline ad-hoc fake.
- The SSH `key_file` test uses a `~`-relative path so `expanduser()` is actually
  exercised; `close()` now asserts the provider stays usable; `get_host`/created
  -host tests assert host name and id derivation instead of `is not None`.
- Local tag tests assert full-dict equality; the legacy-volume tests use a
  unique `uuid4` directory name; `get_volume_for_host` asserts the volume is
  rooted at the host data dir (and round-trips a file through it).
- Backend `get_description`/`*_args_help` tests pin the real strings via
  snapshots / distinguishing content instead of "non-empty string" checks.
- Removed duplicate tests: a redundant local default-host-dir test and two
  sshd-backed acceptance tests that only re-verified pure-Python id derivation
  already covered by unit tests. Capability-flag tests gained
  intent/cross-reference comments.
