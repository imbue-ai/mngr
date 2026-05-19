Latchkey gateway ships a new bundled `minds-api-proxy` extension that
transparently reverse-proxies requests under `/extensions/minds-api-proxy`
to the minds desktop client's bare-origin "Minds API". The upstream URL
is read at request time from the `LATCHKEY_EXTENSION_MINDS_API_URL`
environment variable, and is published to the detached
`mngr latchkey forward` supervisor (via the new
`LatchkeyForwardSupervisor.extra_env`) on every `minds run` startup, so
the proxy always points at the live Minds API port even when minds
re-binds on restart. The extension responds 503 when the env var is not
configured; requests still go through the gateway's normal permission
check.
