Added `ModalInterface.is_function_deployed`, which answers whether an app's current deployment publishes a function of a given name. Unlike looking a function up and reading its web URL, it answers once rather than riding out Modal's post-deploy read-consistency window, so an absent function is reported immediately instead of after a retry budget. `mngr_modal` uses it to decide whether an app already carries a route endpoint.

`ModalInterface.deploy` now takes `extra_env`, added to the environment the deployed script runs under, for scripts that read their configuration from the environment.
