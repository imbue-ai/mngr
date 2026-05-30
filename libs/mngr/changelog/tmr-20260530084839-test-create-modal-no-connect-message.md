Fixed the `test_create_modal_no_connect_message` release e2e test, which had
started failing with "No agent type provided". Since the `--type` default was
moved out of source code and into user config (set by the installer), the
isolated e2e profile no longer supplied a default agent type. The test now
passes `--type claude` explicitly, matching the tutorial's intent that the
default agent type is `claude`.
