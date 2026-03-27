# Core `mng` principles

These are the core product principles that guide the overall design.

`mng` should be:

1. **direct**: `mng`'s commands should do exactly what you tell them to do, with minimal abstraction or "magic" in between. You should always be able to see and understand what is happening under the hood.
2. **immediate**: `mng`'s commands and interface should be fast and responsive. We want to minimize wait times and make it feel like you're directly interacting with your hosts.
3. **safe**: `mng`'s commands should prioritize safety and reliability. Operations should be designed to avoid data loss, corruption, or unintended side effects.
4. **personal**: `mng`'s commands should serve *only* the user. Your data and hosts are yours alone, and `mng` should never share or expose them without your explicit permission. There are no team features, etc. for this reason, and all data collection should be explicitly configured.
