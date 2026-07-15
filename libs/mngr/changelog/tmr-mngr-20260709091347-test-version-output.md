Fixed the fresh-install test fixture (`isolated_mngr_venv`) to install the `overlay` workspace package. Without it, every `mngr` command in a freshly installed venv crashed with `ModuleNotFoundError: No module named 'imbue.overlay'`, causing the install tests to fail.

Strengthened `test_version_output` to assert that `mngr --version` actually prints a version string (not just the program name).
