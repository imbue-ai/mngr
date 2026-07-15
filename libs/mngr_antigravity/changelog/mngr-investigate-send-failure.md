# Antigravity: marker-based message delivery confirmation

- Changed: `mngr message` to an antigravity agent now confirms submission by polling the `active` marker (maintained by mngr's statusLine command on every busy sample) advancing past its pre-Enter state, instead of waiting on the `mngr-submit-<session>` tmux `wait-for` signal. tmux latches a signal fired with no waiter, which could instantly false-confirm a later send (and kill its pending Enter keystroke); the marker gives the same timing without that failure mode, and it exists on agents created by older mngr versions, so no reprovisioning is needed.

- Changed: the statusLine still fires the (now-unlistened) `mngr-submit` signal so older mngr senders keep confirming; the signal is marked for removal in a future release.

- Added: a message-delivery journey release test (idle -> busy -> rapid sequential -> long buffer-pasted message, each delivered exactly once).
