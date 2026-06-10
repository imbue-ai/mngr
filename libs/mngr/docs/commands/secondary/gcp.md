<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr gcp
**Usage:**

```text
mngr gcp [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr gcp prepare

**Usage:**

```text
mngr gcp prepare [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--project` | text | GCP project ID. Defaults to the provider config's project_id (must be set somewhere). | None |
| `--zone` | text | GCE zone for the client. Firewall rules are global, but the client is zone-bound; defaults to us-west1-a. | None |
| `--firewall-name` | text | Firewall rule name to create / reuse. Defaults to 'mngr-gcp-ssh'. | None |
| `--firewall-target-tag` | text | Network tag the rule targets (every instance is tagged with it). Defaults to 'mngr-ssh'. | None |
| `--network` | text | VPC network the rule applies to. Defaults to 'default'. | None |
| `--allowed-ssh-cidr` | text | Inbound CIDR allowed on tcp/22 and tcp/<container_ssh_port>. Repeat for multiple. Required (fail-closed): with none supplied, prepare refuses to create a wide-open rule. | None |
