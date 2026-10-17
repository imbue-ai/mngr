- `mngr list` reports an AWS host's real size (the instance type's vCPUs and RAM from `ec2:DescribeInstanceTypes`, and the root volume size), recorded on the host record at create, instead of the 1 CPU / 1 GB placeholder. Hosts created before this release answer from a table of the instance types the Imbue Studio create form offers.

- The root volume size knob is now `root_disk_size_gb` (shared with GCP and Azure). `root_volume_size_gb` keeps working as a deprecated alias; setting both to different values is a config error.

- The AWS release trip asserts the size `mngr list` reports matches what EC2 says the instance type is.
