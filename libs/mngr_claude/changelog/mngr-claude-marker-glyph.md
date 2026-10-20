Claude's streaming output now reaches the stream buffer in terminals where Claude draws the content-block marker as U+23FA BLACK CIRCLE FOR RECORD rather than U+25CF BLACK CIRCLE.

The watcher recognised only U+25CF, so in those terminals no pane line was ever assistant text: region extraction returned nothing on every poll, the buffer kept its id line and never gained a body, and streaming was silently off for the agent's whole life. Which glyph Claude uses depends on the terminal it detects, not on its version, which is why this affected macOS while Linux was unaffected.
