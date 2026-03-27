# modal-proxy

Abstraction layer over the [Modal](https://modal.com) SDK for [mngr](../mngr/README.md).

This library defines a `ModalInterface` ABC that captures every interaction mngr_modal has with Modal. Three implementations are planned:

1. **DirectModalInterface** -- calls the Modal Python SDK directly (the current behavior, extracted from mngr_modal)
2. **TestingModalInterface** -- fakes Modal behavior locally (volumes become directories, sandboxes become process groups) for integration testing without remote calls
3. **RemoteModalInterface** -- proxies calls to a web server, enabling a managed service that translates user credentials into real Modal API calls

## Usage

The `ModalInterface` is intended to be injected into `ModalProviderInstance` (in mngr_modal) rather than having mngr_modal call the Modal SDK directly.
