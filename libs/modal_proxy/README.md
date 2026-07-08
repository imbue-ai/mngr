# modal-proxy

An abstraction layer over the [Modal](https://modal.com) Python SDK.

`ModalInterface` is an abstract base class that mirrors the Modal object model (App, Sandbox, Image, Volume, etc.), exposing a focused subset of operations. Modal-specific exceptions are translated into `ModalProxy*` errors at the boundary, so callers never need to import `modal` directly.

The provided `DirectModalInterface` implementation wraps the real Modal SDK. Inject a `ModalInterface` instance into your own code rather than calling the Modal SDK directly; this decouples your application from the SDK and makes it straightforward to substitute a fake implementation in tests.
