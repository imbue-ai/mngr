# ty 0.0.39 / paramiko 4.0 type fixes

- Converted bracketed `# type: ignore[...]` suppressions to `# ty: ignore[...]` (test files), since `ty` 0.0.39 no longer honors the mypy-style bracketed form.
- The new `types-paramiko` stubs (pulled in by the paramiko 4.0 bump) surfaced an intentional Liskov-violating `get_transport` override in the SSH-tunnel test fake (`FakeSSHClient`); this is now annotated with `# ty: ignore[invalid-method-override]`.

- Tightened this project's `test_ratchets.py` violation counts to their exact current values (`--inline-snapshot=trim`).

Test-only changes; no user-facing behavior change.
