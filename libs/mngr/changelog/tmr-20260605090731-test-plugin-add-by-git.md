Strengthened the `test_plugin_add_by_git` e2e tutorial test. It now asserts the
command exits with code 1 and emits a clean "Aborted:" message (rather than just
a non-zero exit code), confirming that `--git` is accepted as a source specifier
and the command reaches an intentional error path instead of a click usage error
or an uncaught traceback.
