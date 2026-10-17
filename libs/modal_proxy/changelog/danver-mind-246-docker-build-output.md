Failed image builds can now be asked for their logs.

`ImageInterface.fetch_build_logs()` returns the build output of an image's final layer, fetched from Modal rather than read off the live log stream. Call it on the image that failed to build: Modal resolves it through the failed build attempt it keeps on that object. Modal indexes a failed build's log a few seconds behind the failure and fills it in a piece at a time, so the call waits (up to 15 seconds) for the build to show as terminated before returning.

Modal gives no structural end-of-log signal on that path -- the batches' `eof` and `app_done` flags and the entries' `task_state` all come back unset, and its public log API drops them anyway -- so the wait keys off `BUILD_TERMINATION_MARKER`, the line Modal's builder writes as it terminates a failed task. An acceptance test holds Modal to that line, and giving up waiting logs a warning naming the marker, so a rewording upstream surfaces as a failed test rather than as build logs quietly arriving truncated.

Note that `fetch_build_logs` is abstract, so any implementation of `ImageInterface` outside this package has to grow one. The bundled `FakeImage` gained fields for the two things a build failure can independently produce -- what it streamed, and what Modal will hand over if asked -- so a fake can pose the case where those differ.

`modal.exception.ImageBuildError` now translates to a new `ModalProxyImageBuildError` instead of the general `ModalProxyRemoteError`, so callers can tell a build failure apart from other remote failures and know there are build logs to go and get.
