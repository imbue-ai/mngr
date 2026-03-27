# Future work

The following concerns and features are out of scope for the initial versions of mng, but may be addressed in future releases:

- **Credential scoping**: How do we prevent a child agent from using the parent's full credentials? The answer mentions "scoped-down credentials" but this is deferred.
- **Resource accounting**: If agent A spawns agent B, how is resource usage tracked and limited?
- **Secret management**: How are sensitive environment variables and files handled securely? How are API keys rotated?

