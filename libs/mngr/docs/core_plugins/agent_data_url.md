# agent_data_url Plugin [future]

The `agent_data_url` plugin provides a way to access agent-specific data via a URL.

In particular, it exposes all files in the agent's state directory, and provides additional endpoints for treating logs as streams.

**Note**: because this data is served from within the host, it is only accessible when the host is running.
