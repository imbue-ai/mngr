# host_data_url Plugin [future]

The `host_data_url` plugin provides a way to access host-specific data via a URL.

In particular, it exposes all files in the host's state directory, and provides additional endpoints for treating logs as streams.

Host logs *only* include those that happen while the host is online! For logs about host creation and destruction, see each individual provider's documentation.

**Note**: because this data is served from within the host, it is only accessible when the host is running.
