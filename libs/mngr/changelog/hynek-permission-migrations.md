Fixed two config loader tests that called `_isolate_load_config_env`, the former name of the shared `isolate_load_config_env` test helper, and so failed with a `NameError`. No behavior change.
