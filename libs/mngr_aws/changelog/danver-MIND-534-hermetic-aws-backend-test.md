Fixed a flaky unit test, `backend_test.py::test_get_host_resources_answers_a_stopped_host_from_its_mirrored_record_without_any_ec2_call`. It made a real `sts:GetCallerIdentity` call over the network, so it passed on a fast network and hit the 10-second `pytest-timeout` on a slow one.

The cause was the `_build_stubbed_provider` test helper. It left the provider's S3 state bucket unresolved, and resolving it derives the bucket name from the AWS account id, which takes a live STS call. The helper now starts the provider with no state bucket. Its boto3 session also fails the test on any AWS request that would leave the process, so a stubbed test that reaches the network now fails the same way on every network.

Tests only; the provider behaves as before.
