`FakeImage.dockerfile_commands` now records the command batches it is given, as `applied_command_batches`, so a test can assert what each simulated layer was built from rather than only that an image came back.

`ImageInterface.dockerfile_commands` takes `build_args`, the values for the ARG instructions its commands declare, and forwards them to Modal. `FakeImage` records them as `applied_build_args`.
