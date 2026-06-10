- Pin `cryptography<47` in `libs/mngr` to dodge a SIGILL in the
  `cryptography.hazmat.bindings._rust openssl` import on Apple Silicon M5
  hosts under lima-VZ (47.0.0+ shipped a Rust OpenSSL binding whose code
  uses a CPU instruction lima-VZ on M5 does not expose to the guest).
  46.0.x imports cleanly. Empirically bisected on the minds launch-to-msg
  self-hosted runner.
