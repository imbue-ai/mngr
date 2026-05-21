## AWS provider support: shared int-env-var parser

Lifted `parse_int_env` (previously a private helper in `mngr_claude_subagent_proxy.hook_io`) into `imbue_common.env_vars`, with overloads for `default=None` and `default=int`. Used by the AWS provider for its release-test TTL override and by the existing `mngr_claude_subagent_proxy` call sites.
