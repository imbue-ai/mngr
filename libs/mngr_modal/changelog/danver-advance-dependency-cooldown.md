Added `register_modal_test_fixtures`, which registers this package's Modal test fixtures into a consuming package's conftest namespace -- the same shape as mngr's `register_plugin_test_fixtures`.

It replaces `pytest_plugins = ["imbue.mngr_modal.conftest"]`, which pytest 9.1 refuses to honour outside the rootdir conftest. mngr_claude was the only consumer.
