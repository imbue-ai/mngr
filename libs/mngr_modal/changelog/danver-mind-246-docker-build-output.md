`mngr create` now shows you why your Dockerfile build failed, even when Modal tells it nothing.

Modal streams a build's output as it happens, and that stream was the only thing `mngr create` had to report when a build failed. The stream is best-effort: when Modal's build-failure result is ready before it opens the stream, nothing is streamed at all. `mngr create` would then fail with only Modal's bare "Image build for im-xxxx failed. View the build logs: modal image logs im-xxxx" -- no sign of which line of your Dockerfile broke, which is the one thing you needed.

In that case `mngr create` now asks Modal for the failing layer's output and reports it, both in the error it prints and in the failed host record that `mngr list` reads. A build whose output did stream is left alone, so it is not printed back to you a second time.
