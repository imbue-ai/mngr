Made the "allowed_ssh_cidrs is empty" error (raised when auto-creating a security group
with no inbound CIDRs configured) actionable. It previously suggested the value in Python
syntax (`('203.0.113.4/32',)`, `ExistingSecurityGroup(id='sg-...')`), which isn't runnable.
It now prints a copy-pasteable command that grabs your current public IP and sets it at
user scope:

    mngr config set providers.<name>.allowed_ssh_cidrs "[\"$(curl -fsS https://checkip.amazonaws.com)/32\"]" --scope user

The `<name>` is the actual provider instance name (threaded through as a new
`AwsVpsClient.provider_name` field), and the existing-security-group alternative is now
shown in TOML form (`security_group = { kind = "existing", id = "sg-..." }`).
