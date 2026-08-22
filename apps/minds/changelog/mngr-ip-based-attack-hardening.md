# Deploy checklist: signup IP hardening prerequisites

- Added the signup-IP-hardening items to the next-deployment checklist (`docs/deploy/next_deploy.md`): add the new `IPINFO_TOKEN` key to every tier's Vault `supertokens` entry before deploying (the deploy aborts on a missing template-declared key; staging/production get the IPinfo Max token), and spot-check the access log's `client_ip` field behind the custom domains after the first deploy.
