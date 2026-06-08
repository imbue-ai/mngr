Excluded the new opt-in live Claude Agent SDK test suite from CI by adding `and not sdk_live`
to both pytest filter expressions in `offload-modal.toml`. Added a `just test-sdk-live` recipe
that sets `RUN_SDK_LIVE_TESTS=1` and runs the `sdk_live`-marked tests in `libs/mngr_robinhood`.
