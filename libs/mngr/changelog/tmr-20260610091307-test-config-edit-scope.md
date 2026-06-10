Strengthened the `test_config_edit_scope` e2e tutorial test for `mngr config
edit --scope project`: it now confirms the project-scope config file does not
exist before the edit, making the existing "creates it from a template"
assertion meaningful rather than potentially passing on a pre-existing file.
