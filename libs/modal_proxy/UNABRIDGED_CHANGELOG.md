# Unabridged Changelog - modal_proxy

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/modal_proxy/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-08

- modal_proxy: `ModalInterface.enable_output_capture` is now an abstract method. `DirectModalInterface` hooks into the Modal SDK output system; `TestingModalInterface` returns a `nullcontext`. Stacked on #1520.
