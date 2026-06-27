Fixed AWS workspaces failing to start ("workspace unresponsive"). On the AWS
provider path the container's `/run` lives on gVisor's gofer-backed filesystem,
which rejects `os.link()` of a socket inode with `EOPNOTSUPP`. supervisord
installs its control socket via a hard link, so on AWS that link failed forever
("Unlinking stale socket"), and supervisord never started `system_interface` or
any other service. The per-region `[providers.aws-<region>]` blocks minds writes
now include `default_start_args = ["--tmpfs", "/run"]`, mounting `/run` as a
tmpfs where the hard link succeeds. Scoped to the AWS blocks only; the
ovh/vultr/imbue_cloud paths (which already provide a tmpfs `/run`) are unchanged.
