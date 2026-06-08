Minds bootstrap now writes the gVisor runtime settings into each per-account
`[providers.imbue_cloud_<slug>]` block it registers: `docker_runtime = "runsc"`,
`install_gvisor_runtime = true`, and
`default_start_args = ["--workdir=/", "--security-opt=no-new-privileges"]`. This
makes the imbue_cloud slow (rebuild) path run the agent container under gVisor
with the runsc hardening args, mirroring the forever-claude-template
`[providers.ovh]` bake settings. No user-visible change to the create flow.

Added a `--no-recycle` flag to `minds pool create` that forwards `--no-recycle`
to the admin command, forcing a fresh OVH VPS order instead of reclaiming a
cancelled one (useful for testing the fresh-provision path).

Fixed two JinjaX template bugs where a component tag had a quoted attribute
containing `{{ ... }}` (which JinjaX forwards literally instead of interpolating):
the Landing page's settings-gear `<Button onclick="...{{ agent_id }}...">` (which
navigated to a literal `/workspace/{{ agent_id }}/settings` and then 500'd the
destroy with "AgentId must start with 'agent-', got '{{ agent_id }}'") and the
Sharing page's `<Link href="...{{ agent_id }}...">` (dead "open workspace" link).
Both now use the `attr={{ expr }}` form. Added render regression tests asserting
no literal `{{` survives in the Landing / Workspace-settings / Sharing pages.
