Sharing now works for services served under a base path, like the openvscode editor.

- Enabling sharing on a service whose backend URL carries a path (openvscode registers `http://localhost:8082/service/openvscode`, since it runs with `--server-base-path` so the WebSocket its Web Worker opens gets the prefix baked in server-side) failed with a Cloudflare Access 1056: "ingress rules don't support proxying to a different path on the origin service". The origin is now registered path-less and the path is moved onto the public URL instead, which is exactly equivalent because Cloudflare preserves the request path. Path-less services are unaffected.

- The sharing status route re-derives that base path from the agent's reported backend URL, so the link survives a reload; the connector only stores the path-less origin, and without a resolver the URL degrades to the bare hostname.
