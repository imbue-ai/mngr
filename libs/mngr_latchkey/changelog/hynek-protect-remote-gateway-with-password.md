The VPS-resident latchkey gateway now starts with the same shared password the
local desktop gateway uses. The desktop-derived gateway password (a pure
function of the shared Latchkey encryption key, as produced by
`Latchkey.derive_gateway_password`) is injected into the remote gateway as
`LATCHKEY_GATEWAY_LISTEN_PASSWORD`, matching the `LATCHKEY_GATEWAY_PASSWORD`
agents already present. Previously the remote gateway started without any
listen password, so it did not enforce the same authentication as the local
gateway.
