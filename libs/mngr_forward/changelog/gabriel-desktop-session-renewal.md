A session with `mngr forward` no longer ends 30 days after sign-in while it is still in use.

Before, a signed session cookie was accepted for 30 days from when it was signed and was only re-issued by a page navigation through the `/goto/` bridge. A page left open on an agent origin for longer than that had its background requests refused with 403 and its WebSockets closed, until it was reloaded.

Every HTTP response to a request that presented a valid signed session cookie now carries a freshly signed one with the same scope and attributes. An expired or altered cookie is never renewed, a preauth cookie value is left in place, and WebSocket handshakes do not renew.
