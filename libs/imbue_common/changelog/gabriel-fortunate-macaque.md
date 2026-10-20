Submitting an error report no longer risks crashing the whole process.

The Sentry S3 attachment uploader used to share one `boto3` client across its upload thread pool. A boto3 client owns one urllib3 connection pool and therefore one `SSLContext`, and urllib3 calls `load_verify_locations()` on that context for every new connection it opens -- mutating the context's OpenSSL `X509_STORE` while other threads read the same store, unlocked, to verify the server's certificate. A report that uploads several logs at once opens all of its connections at the same moment, so the uploads raced and could segfault the process (in minds this killed the backend and surfaced as "Minds stopped unexpectedly" right after submitting a report).

Each upload thread now creates and reuses its own S3 client, giving every TLS handshake its own certificate store with no concurrent writer. Uploads still run in parallel; the client is created lazily on each thread's first upload rather than eagerly at Sentry setup.

Those clients are built from a session the uploader owns, instead of the process-global default session that plain `boto3.client()` resolves. Other modules build clients off that global session without locking, so the uploader's lock could never have made it safe; owning the session makes the lock cover the whole hazard.

The upload pool is also pinned to a fixed 8 workers rather than the thread-pool default of `min(32, cpu_count + 4)`. A report's attachment sweep is on the order of ten files, so eight still clears one in a round or two, and the number of live clients (each about 1MB once it has connected) no longer varies with the machine.
