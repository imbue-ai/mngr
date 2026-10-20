The generated ssh config in the latchkey end-to-end test now quotes its `IdentityFile` and `UserKnownHostsFile` paths.

ssh tokenizes each config line on whitespace exactly as it does an `-o` value, so a temp directory containing a space would have made the test's ssh config point at several nonexistent files.
