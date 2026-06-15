Added **AWS** as a compute-provider option in the workspace create form, alongside Docker, Lima, Vultr, and Imbue Cloud. Selecting AWS launches the workspace in a runsc-hardened Docker container on an Amazon EC2 instance (the same outer-host/container model as the Vultr and OVH providers, so the secure latchkey gateway runs on the EC2 host outside the agent's container).

- The create form requires picking an AWS region (from the regions with pinned default AMIs) and shows an inline note that AWS credentials are read from the environment (`AWS_*` / `AWS_PROFILE` / `~/.aws`).

- The existing "Cloud" compute option was renamed to "Vultr" to name its provider plainly.

- The workspace listing now shows a compute-provider label on every row (AWS, Vultr, Docker, Lima, Imbue Cloud).

- AWS hosts are long-lived: they never idle-shut-down and have no max-lifetime timer.

- minds writes one per-region AWS provider block into its mngr settings at startup (only when AWS credentials are present) and ensures the region's security group exists before each create.

- minds now suppresses the default region-less `aws` provider in its mngr settings (the same way it already suppresses the default `imbue_cloud` provider), so `mngr list` no longer logs a spurious "credentials not configured" discovery warning every cycle. The usable AWS providers remain the per-region `aws-<region>` blocks.
