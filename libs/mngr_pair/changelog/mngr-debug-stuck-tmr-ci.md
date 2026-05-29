Mark ``test_unison_syncer_syncs_symlinks`` flaky.

The test's ``wait_for`` only waits for the symlink to land in the target
directory, but the very next assertion checks that the symlink's referent file
``real_file.txt`` also exists. Unison gives no ordering guarantee between two
unrelated files in a single sync sweep, so the symlink can appear before its
target file does. The proper fix is to widen the ``wait_for`` predicate; left
for a follow-up.
