Added `test_every_mngr_plugin_isolates_home_in_tests` to `test_meta_ratchets.py`:
every mngr plugin (any project with a `[project.entry-points.mngr]` table) must
call `register_plugin_test_fixtures(globals())` in a conftest, guaranteeing its
tests redirect $HOME away from the developer's real home directory.
