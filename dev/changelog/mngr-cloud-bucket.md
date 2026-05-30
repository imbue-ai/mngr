Added the implementation spec for Imbue Cloud R2 bucket support
(`specs/imbue-cloud-r2-buckets/spec.md`).

Updated the `.minds/template/cloudflare.sh` secret template to document that
`CLOUDFLARE_API_TOKEN` must now be an account-owned (`cfat_`) token carrying
`Workers R2 Storage: Edit` + `Account API Tokens: Edit` (on top of the existing
tunnel/DNS/Access/KV permissions), and that R2 must be enabled on the Cloudflare
account.
